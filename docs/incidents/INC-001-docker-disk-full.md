# INC-001: Host disk filled during an Oracle restore test; Docker VM failed

| | |
|---|---|
| Date | 2026-11-17 |
| Engine | All (Docker Desktop VM); triggered by Oracle |
| Drill | None: a real incident while building M3 |
| Severity | SEV-2 (whole lab down; no data lost) |
| Time to detect | about 1 minute |
| Time to recover | about 60 minutes, most of it freeing disk space |

## What users would have seen

Every database unreachable, or failing to restart: 4 of 8 containers stopped,
and Docker itself refusing to start, remove or list containers.

## Timeline

| Time (UTC) | Event |
|---|---|
| 14:38 | First run of `scripts/backup/oracle.sh`: backup (439 MB) and `RESTORE VALIDATE` pass |
| 14:40 | Full restore starts on a scratch container, writing about 3 GB of datafiles |
| 14:41 | Restore fails: `ORA-03113` (end-of-file on communication) mid-restore |
| 14:41 | `docker exec` returns `input/output error`; `docker system df` cannot read image blobs |
| 14:42 | Host disk checked: 1.2 GB free, data volume at 100% |
| 14:43 | Test backups (0.5 GB) deleted; not enough |
| 15:02 | 9.8 GB of unused ML models deleted from the Hugging Face cache, by the owner's decision |
| 15:05 | `docker desktop restart`; MariaDB, PostgreSQL, Oracle come back on their own |
| 15:07 | Mongo containers fail with `failed to start shim ... file exists`; recreated with `--force-recreate`; volumes intact |
| 15:08 | `dbops health`: 57 OK. Backups re-run with a free-space check |

## Root cause

Docker Desktop keeps its whole VM disk in one sparse file on the Mac's disk. A
restore inside a container grows that file. The Mac had about 4 GB free before
the run; the 3 GB restore plus the backup used it up, and the VM's filesystem
started failing writes. Oracle was the process writing at that moment, so its
error showed first, but every container was affected.

Contributing: the backup scripts had no free-space check, and nothing in the lab
watched the host disk. `dbops health` reports storage for each container's data
directory, but inside the VM that reads 453 GB total and 4% used: the VM cannot
see that the real disk underneath is full.

## How it was detected

```
$ docker exec dbops-oracle true
open /var/lib/docker/rootfs/overlayfs/.../etc/passwd: input/output error

$ df -h /System/Volumes/Data
/dev/disk3s5   460Gi   429Gi   1.2Gi   100%
```

## Fix

1. Delete what the lab itself had written (test backups, the scratch container).
2. Free space on the host: unused cached models, 9.8 GB.
3. `docker desktop restart`.
4. `docker compose up -d --force-recreate mongo1 mongo2 mongo3` for the containers
   whose runtime state was left half-written.
5. `make health` to confirm, then re-run every backup and restore test.

## What would stop it happening again

- [x] Every backup script checks host free space before writing anything
      (`require_free_gb` in `scripts/backup/lib.sh`): 8 GB for the Oracle full
      restore, 2-3 GB for the others.
- [x] Runbook entries for `input/output error` and `failed to start shim`.
- [ ] A host-disk check in `dbops health`, reading the Mac's free space rather
      than the VM's (the VM's figure is what hid this).
- [ ] Shrink the Oracle restore test: skip the PDB seed's datafiles, which the
      test does not need.
