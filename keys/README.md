# Update signing keys

Place the **Ed25519 public key** used to verify release manifests here:

```
keys/update_public.pem
```

Generate a key pair (CI holds the private key only):

```bash
openssl genpkey -algorithm Ed25519 -out update_private.pem
openssl pkey -in update_private.pem -pubout -out update_public.pem
```

Sign a canonical manifest (JSON without `signature`, sorted keys, compact):

```bash
# After building vesyl-print-X.Y.Z.manifest.json body:
openssl pkeyutl -sign -inkey update_private.pem \
  -rawin -in manifest.canonical.json \
  | base64 -w0 > signature.b64
```

Ship `update_public.pem` on appliances (this directory or
`/etc/vesyl-print/keys/update_public.pem`). Never ship the private key.
