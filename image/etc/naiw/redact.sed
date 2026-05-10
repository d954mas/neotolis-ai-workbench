# /etc/naiw/redact.sed — Phase 1 redaction patterns (D-14)
s/ghp_[A-Za-z0-9]{30,}/[REDACTED]/g
s/gho_[A-Za-z0-9]{30,}/[REDACTED]/g
s/ghs_[A-Za-z0-9]{30,}/[REDACTED]/g
s/sk-ant-[A-Za-z0-9_-]{30,}/[REDACTED]/g
s/sk-[A-Za-z0-9]{30,}/[REDACTED]/g
s/[Bb]earer [A-Za-z0-9._-]{20,}/[REDACTED]/g
s/AKIA[A-Z0-9]{16}/[REDACTED]/g
