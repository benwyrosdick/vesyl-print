sudo cp printserve-boot.service printserve-display.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable printserve-boot.service printserve-display.service
