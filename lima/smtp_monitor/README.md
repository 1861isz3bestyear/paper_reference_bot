# SMTP bot monitor

This read-only monitor checks the reference and Bybit Demo services, candle cursors, demo halt and
protection state, recent journal errors, and reference-versus-demo position direction. It never
places orders or restarts bots. A condition must persist for 120 seconds before the first email;
unchanged alarms repeat hourly, and recovery generates one email.

Create the credentials file before using the main installer:

```bash
cd ~/paper_reference_bot/lima/smtp_monitor
cp smtp_monitor.env.example smtp_monitor.env
chmod 600 smtp_monitor.env
```

Set port 465 with `SMTP_SECURITY=ssl`, or port 587 with `SMTP_SECURITY=starttls`. No client SSL
certificate is required. The installer adds `smtp-monitor.service` and `smtp-monitor.timer`; the
timer starts shortly after activation and checks once per minute. An issue must remain present for
two minutes before it is mailed.

Inspect it with:

```bash
systemctl --user status smtp-monitor.timer --no-pager
journalctl --user-unit smtp-monitor.service --since "1 hour ago" --no-pager -o cat
systemctl --user start smtp-monitor.service
```
