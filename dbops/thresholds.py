"""Warning and critical thresholds. One place to tune them, one place to read them."""

# Percent of the limit in use
CPU_PCT = (80, 95)
MEMORY_PCT = (80, 90)
STORAGE_PCT = (80, 90)            # filesystem holding the data directory
CONNECTIONS_PCT = (80, 95)        # of max_connections / sessions
TABLESPACE_PCT = (85, 95)         # Oracle, against max size (autoextend counted)

# Seconds
REPLICATION_LAG_S = (30, 300)
LONG_RUNNING_S = 60               # a statement active this long counts as long-running
LONG_RUNNING_COUNT = (1, 5)       # how many long-running statements before WARN / CRIT

# Count
BLOCKED_SESSIONS = (1, 5)
