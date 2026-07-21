---
name: Bug report
about: Something didn't work against your iDRAC6
title: ""
labels: bug
---

**Your hardware/firmware**
- Server model (e.g. R610, R710):
- iDRAC6 card type (Enterprise or Express):
- Firmware version before you ran the script (from `--check` or the iDRAC web UI):

**`--check` output**

Run `python idrac_flash.py .env --check` and paste the full output here. This
is read-only, it doesn''t touch your firmware, safe to run and paste even if
you''re just reporting an oddity, not a failed flash.

```
(paste here)
```

**What happened**

What you ran, what you expected, what happened instead.

**`idrac_flash_log.txt`**

If the script got further than `--check` (upload, flash, verify), attach
`idrac_flash_log.txt` from next to the script. It logs the iDRAC''s own
responses, not your password or session cookie/token, so it''s safe to
attach as-is. If you''d rather not post it publicly, mention that and we can
sort out a private way to share it.