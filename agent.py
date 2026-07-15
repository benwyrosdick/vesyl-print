"""Cloud agent: whoami + heartbeat + optional job pull loop."""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone
from typing import Callable

import auth
import jobs
import printers
import statusio
import sysinfo
from cloud import CloudClient, CloudError
from config import AGENT_VERSION, Config, load_config
from jobs import JobError, JobStore, PrintJob

log = logging.getLogger("vesyl-print.agent")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _status_from_creds(
    creds: auth.Credentials | None,
    *,
    pairing: statusio.PairingState = "unpaired",
    cloud: statusio.CloudState = "unknown",
    last_error: str | None = None,
    last_heartbeat_at: str | None = None,
) -> statusio.AgentStatus:
    st = statusio.AgentStatus(
        pairing=pairing,
        cloud=cloud,
        last_error=last_error,
        last_heartbeat_at=last_heartbeat_at,
        agent_version=AGENT_VERSION,
    )
    if creds:
        st.node_id = creds.node_id
        st.name = creds.name
        st.organization_name = creds.organization_name
        st.warehouse_name = creds.warehouse_name
    return st


def _handle_unauthorized(cfg: Config, creds: auth.Credentials | None) -> None:
    """Revoke local pairing: clear credentials, write LCD status. No auto-reclaim."""
    log.warning("device token rejected (401) — re-pair required")
    st = _status_from_creds(creds, pairing="revoked", cloud="offline")
    st.last_error = "re-pair required"
    statusio.write_status(cfg.status_path, st)
    auth.clear_credentials(cfg.credentials_path)


def cloud_job_hooks(
    client: CloudClient, device_token: str
) -> tuple[Callable[[PrintJob], None], Callable[[PrintJob, str, str | None], None]]:
    """Build ack / state callbacks that call wms-api after durable receive."""

    def ack(job: PrintJob) -> None:
        client.ack_job(device_token, job.id)

    def report_state(job: PrintJob, state: str, detail: str | None = None) -> None:
        # Server accepts only done|error from agents (not "printing").
        if state not in ("done", "error"):
            return
        client.report_job_state(
            device_token, job.id, state, message=detail
        )

    return ack, report_state


def run_once(cfg: Config, client: CloudClient | None = None) -> statusio.AgentStatus:
    """Single heartbeat cycle: whoami (best-effort) + heartbeat. Updates status file."""
    client = client or CloudClient(cfg.api_base_url)
    creds = auth.load_credentials(cfg.credentials_path)

    if not creds:
        existing = statusio.read_status(cfg.status_path)
        if existing and existing.pairing == "revoked":
            st = existing
            st.cloud = "offline"
            st.agent_version = AGENT_VERSION
            statusio.write_status(cfg.status_path, st)
            return st
        st = _status_from_creds(None, pairing="unpaired", cloud="unknown")
        statusio.write_status(cfg.status_path, st)
        return st

    try:
        who = client.whoami(creds.device_token)
        creds = auth.merge_whoami(creds, who)
        auth.save_credentials(cfg.credentials_path, creds)
    except CloudError as e:
        if e.unauthorized:
            _handle_unauthorized(cfg, creds)
            return statusio.read_status(cfg.status_path) or _status_from_creds(
                None, pairing="revoked", cloud="offline"
            )
        log.warning("whoami failed: %s", e.message)
    except Exception as e:
        log.warning("whoami failed: %s", e)

    printers_payload = None
    try:
        printers_payload = printers.inventory_payload()
    except Exception:
        log.debug("printer inventory unavailable", exc_info=True)

    try:
        hb = client.heartbeat(
            creds.device_token,
            agent_version=AGENT_VERSION,
            hostname=sysinfo.hostname(),
            printers=printers_payload,
        )
        last_hb = hb.get("last_seen_at") or _utc_now_iso()
        st = _status_from_creds(
            creds,
            pairing="paired",
            cloud="online",
            last_heartbeat_at=str(last_hb),
        )
        statusio.write_status(cfg.status_path, st)
        return st
    except CloudError as e:
        if e.unauthorized:
            _handle_unauthorized(cfg, creds)
            return statusio.read_status(cfg.status_path) or _status_from_creds(
                None, pairing="revoked", cloud="offline"
            )
        log.warning("heartbeat failed: %s", e.message)
        st = _status_from_creds(
            creds,
            pairing="paired",
            cloud="offline",
            last_error=e.message,
        )
        prev = statusio.read_status(cfg.status_path)
        if prev and prev.last_heartbeat_at:
            st.last_heartbeat_at = prev.last_heartbeat_at
        statusio.write_status(cfg.status_path, st)
        return st
    except Exception as e:
        log.warning("heartbeat failed: %s", e)
        st = _status_from_creds(
            creds, pairing="paired", cloud="offline", last_error=str(e)
        )
        statusio.write_status(cfg.status_path, st)
        return st


def drain_local_queue(
    cfg: Config,
    *,
    client: CloudClient | None = None,
    device_token: str | None = None,
) -> None:
    """Crash recovery: finish any jobs left in queue/*.json."""
    store = jobs.store_from_config(cfg)
    pending = store.list_queued_ids()
    if not pending:
        return
    log.info("draining %d queued job(s)", len(pending))

    ack: Callable[[PrintJob], None] = jobs.noop_ack
    report_state: Callable[[PrintJob, str, str | None], None] = jobs.noop_state
    if client is not None and device_token:
        ack, report_state = cloud_job_hooks(client, device_token)

    results = jobs.drain_queue(store, ack=ack, report_state=report_state)
    for job_id, result in results:
        log.info("drain %s → %s", job_id, result)


def pull_and_process(
    cfg: Config,
    client: CloudClient,
    device_token: str,
    store: JobStore | None = None,
) -> str:
    """Pull pending jobs from the cloud and run the durable print pipeline.

    Returns:
      "ok" — pull succeeded (possibly zero jobs)
      "unavailable" — 404 (API not shipped) or 503 (service disabled)
      "unauthorized" — 401
      "error" — other failure
    """
    store = store or jobs.store_from_config(cfg)
    try:
        payloads = client.pending_jobs(device_token)
    except CloudError as e:
        if e.unauthorized:
            return "unauthorized"
        if e.not_found or e.service_disabled:
            log.warning(
                "jobs/pending unavailable (HTTP %s): %s — pull will retry",
                e.status,
                e.message,
            )
            return "unavailable"
        log.warning("jobs/pending failed: %s", e.message)
        return "error"
    except Exception as e:
        log.warning("jobs/pending failed: %s", e)
        return "error"

    if not payloads:
        return "ok"

    log.info("pulled %d job(s)", len(payloads))
    ack, report_state = cloud_job_hooks(client, device_token)

    for payload in payloads:
        try:
            job = PrintJob.from_dict(payload)
        except JobError as e:
            log.error("skip invalid job payload: %s", e.message)
            continue
        try:
            jobs.receive_job(
                job,
                store,
                ack=ack,
                report_state=report_state,
            )
        except JobError as e:
            log.error("job %s failed: %s", job.id, e.message)
        except Exception:
            log.exception("job %s failed unexpectedly", getattr(job, "id", "?"))

    return "ok"


def run_agent(cfg: Config | None = None) -> None:
    """Long-running heartbeat + optional job-pull loop with reconnect backoff."""
    cfg = cfg or load_config()
    cfg.ensure_dirs()
    client = CloudClient(cfg.api_base_url)

    running = {"go": True}
    signal.signal(signal.SIGINT, lambda *_: running.update(go=False))
    signal.signal(signal.SIGTERM, lambda *_: running.update(go=False))

    log.info(
        "agent starting api_base_url=%s heartbeat=%ss pull_jobs=%s pull_interval=%ss",
        cfg.api_base_url,
        cfg.heartbeat_seconds,
        cfg.pull_jobs_enabled,
        cfg.pull_interval_seconds,
    )

    creds = auth.load_credentials(cfg.credentials_path)
    token = creds.device_token if creds else None
    try:
        drain_local_queue(cfg, client=client, device_token=token)
    except Exception:
        log.exception("queue drain failed")

    hb_interval = max(5, int(cfg.heartbeat_seconds))
    pull_interval = max(1, int(cfg.pull_interval_seconds))
    last_hb = 0.0
    backoff = 1.0
    max_backoff = 60.0
    # Sticky: after 404, still retry periodically but don't thrash.
    pull_disabled_until = 0.0

    while running["go"]:
        now = time.monotonic()
        cycle_start = now
        creds = auth.load_credentials(cfg.credentials_path)

        # Heartbeat on its interval (also when unpaired — writes unpaired status).
        do_hb = (now - last_hb) >= hb_interval or last_hb == 0.0
        st: statusio.AgentStatus | None = None
        if do_hb:
            st = run_once(cfg, client)
            last_hb = time.monotonic()
            if st.cloud == "online":
                backoff = 1.0
            elif st.pairing == "paired" and st.cloud == "offline":
                backoff = min(max_backoff, max(backoff, 1.0) * 2)
        else:
            st = statusio.read_status(cfg.status_path)

        # Job pull when enabled, paired, and not in backoff from unavailable API.
        if (
            cfg.pull_jobs_enabled
            and creds
            and (st is None or st.pairing == "paired")
            and time.monotonic() >= pull_disabled_until
        ):
            result = pull_and_process(cfg, client, creds.device_token)
            if result == "unauthorized":
                _handle_unauthorized(cfg, creds)
            elif result == "unavailable":
                # Back off pulls (404/503) without stopping heartbeats.
                pull_disabled_until = time.monotonic() + min(60.0, max(15.0, backoff * 5))
            elif result == "error":
                pull_disabled_until = time.monotonic() + min(30.0, backoff)

        # Sleep: when pulling, wake on pull_interval; else heartbeat interval.
        if cfg.pull_jobs_enabled and creds and (
            st is None or st.pairing == "paired"
        ):
            sleep_for = float(pull_interval)
        elif st and st.pairing == "unpaired":
            sleep_for = min(10.0, float(hb_interval))
        elif st and st.pairing == "revoked":
            sleep_for = min(30.0, float(hb_interval))
        elif st and st.cloud == "offline":
            sleep_for = min(max_backoff, max(float(hb_interval), backoff))
        else:
            sleep_for = float(hb_interval)

        # Don't sleep past next heartbeat if pull interval is long.
        if last_hb > 0:
            until_hb = hb_interval - (time.monotonic() - last_hb)
            if until_hb > 0:
                sleep_for = min(sleep_for, until_hb)

        elapsed = time.monotonic() - cycle_start
        time.sleep(max(0.0, sleep_for - elapsed))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_agent(load_config())


if __name__ == "__main__":
    main()
