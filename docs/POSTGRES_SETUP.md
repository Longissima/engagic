# PostgreSQL Setup for Engagic

Quick setup guide for local development and VPS deployment.

## Local Setup (macOS)

### 1. Install PostgreSQL

```bash
# Using Homebrew (recommended)
brew install postgresql@16

# Start PostgreSQL service
brew services start postgresql@16

# Verify running
psql postgres -c "SELECT version();"
```

###2. Create Database and User

```bash
# Connect to default postgres database
psql postgres

# Create database
CREATE DATABASE engagic;

# Create user with password
CREATE USER engagic WITH PASSWORD 'engagic_dev_password';

# Grant privileges
GRANT ALL PRIVILEGES ON DATABASE engagic TO engagic;

# Connect to engagic database
\c engagic

# Grant schema privileges (PostgreSQL 15+)
GRANT ALL ON SCHEMA public TO engagic;

# Exit
\q
```

### 3. Configure Environment

Create `.env` file in project root:

```bash
# PostgreSQL connection
ENGAGIC_USE_POSTGRES=true
ENGAGIC_POSTGRES_HOST=localhost
ENGAGIC_POSTGRES_PORT=5432
ENGAGIC_POSTGRES_DB=engagic
ENGAGIC_POSTGRES_USER=engagic
ENGAGIC_POSTGRES_PASSWORD=engagic_dev_password

# Connection pool
ENGAGIC_POSTGRES_POOL_MIN_SIZE=10
ENGAGIC_POSTGRES_POOL_MAX_SIZE=100

# Other config (keep existing)
GEMINI_API_KEY=your_key_here
ENGAGIC_ADMIN_TOKEN=your_token_here
```

### 4. Load Environment

```bash
# Install python-dotenv if not already
uv pip install python-dotenv

# Or manually export
export $(cat .env | xargs)
```

### 5. Initialize Schema

```bash
# Python test script
python3 -c "
import asyncio
from database.db_postgres import Database

async def init():
    # Bootstrap is the only supported reason to bypass the read-only
    # schema-current startup gate.
    db = await Database.create(require_current_schema=False)
    await db.init_schema()
    print('Schema initialized!')
    await db.close()

asyncio.run(init())
"

# Record/apply every versioned migration before starting application code.
python3 -m database.migrate
```

### 6. Verify Setup

```bash
# Check tables created
psql engagic -c "\dt"

# Should see:
# - cities
# - meetings
# - meeting_topics
# - items
# - item_topics
# - city_matters
# - matter_topics
# - matter_appearances
# - queue
# - cache
# - (tenant tables)

# Check indexes
psql engagic -c "\di"
```

---

## VPS Setup (Ubuntu/Debian)

### 1. Install PostgreSQL

```bash
# SSH to VPS
ssh root@engagic

# Install PostgreSQL
sudo apt update
sudo apt install postgresql postgresql-contrib

# Start service
sudo systemctl start postgresql
sudo systemctl enable postgresql
```

### 2. Create Database and User

```bash
# Switch to postgres user
sudo -u postgres psql

# Create database
CREATE DATABASE engagic;

# Create user
CREATE USER engagic WITH PASSWORD 'STRONG_PASSWORD_HERE';

# Grant privileges
GRANT ALL PRIVILEGES ON DATABASE engagic TO engagic;

# Connect and grant schema access
\c engagic
GRANT ALL ON SCHEMA public TO engagic;

\q
```

### 3. Configure PostgreSQL for Remote Access (if needed)

```bash
# Edit postgresql.conf
sudo nano /etc/postgresql/16/main/postgresql.conf

# Change:
listen_addresses = 'localhost'  # Or '*' for all interfaces

# Edit pg_hba.conf
sudo nano /etc/postgresql/16/main/pg_hba.conf

# Add (ONLY if accessing remotely - use SSH tunnel instead if possible):
# host    engagic    engagic    0.0.0.0/0    scram-sha-256

# Restart PostgreSQL
sudo systemctl restart postgresql
```

### 4. Set Environment Variables

```bash
# Add to ~/.bashrc or /etc/environment
export ENGAGIC_USE_POSTGRES=true
export ENGAGIC_POSTGRES_HOST=localhost
export ENGAGIC_POSTGRES_PORT=5432
export ENGAGIC_POSTGRES_DB=engagic
export ENGAGIC_POSTGRES_USER=engagic
export ENGAGIC_POSTGRES_PASSWORD="STRONG_PASSWORD_HERE"
```

### 5. Initialize Schema

```bash
cd /root/engagic
python3 -c "
import asyncio
from database.db_postgres import Database

async def init():
    # Bootstrap is the only supported reason to bypass the read-only
    # schema-current startup gate.
    db = await Database.create(require_current_schema=False)
    await db.init_schema()
    print('Schema initialized!')
    await db.close()

asyncio.run(init())
"

python3 -m database.migrate
```

---

## Testing Connection

### Quick Test Script

Save as `test_postgres.py`:

```python
import asyncio
from database.db_postgres import Database
from database.models import City

async def test():
    print("Creating connection pool...")
    db = await Database.create()

    print("Testing city operations...")

    # Add test city
    test_city = City(
        banana="testCA",
        name="Test City",
        state="CA",
        vendor="legistar",
        slug="test-ca",
        zipcodes=["94301"]
    )

    await db.add_city(test_city)
    print("✅ City added")

    # Retrieve city
    retrieved = await db.get_city("testCA")
    print(f"✅ City retrieved: {retrieved.name}")

    # List all cities
    cities = await db.get_all_cities()
    print(f"✅ Found {len(cities)} cities")

    await db.close()
    print("✅ All tests passed!")

if __name__ == "__main__":
    asyncio.run(test())
```

Run:
```bash
python3 test_postgres.py
```

---

## Troubleshooting

### Connection Refused

```bash
# Check if PostgreSQL is running
sudo systemctl status postgresql

# Check port
sudo netstat -plunt | grep postgres

# Check logs
sudo tail -f /var/log/postgresql/postgresql-16-main.log
```

### Permission Denied

```bash
# Verify user exists
psql postgres -c "\du"

# Re-grant permissions
psql engagic -c "GRANT ALL ON SCHEMA public TO engagic;"
```

### Schema Not Found

```bash
# Check if schema file exists
ls -la /Users/origami/engagic/database/schema_postgres.sql  # Local
ls -la /root/engagic/database/schema_postgres.sql  # VPS

# Manual schema init
psql engagic < database/schema_postgres.sql
```

---

## VPS Performance Tuning (2GB RAM)

PostgreSQL defaults are designed for servers with 4GB+ RAM. For optimal performance on a 2GB VPS, apply these tuning parameters.

### Automated Tuning (Recommended)

We provide a tuning script that automatically configures PostgreSQL for 2GB VPS:

```bash
# Run tuning script
sudo ./scripts/tune_postgres.sh
```

The script will:
1. Backup current configuration
2. Apply memory-optimized settings
3. Enable slow query logging
4. Restart PostgreSQL
5. Verify connection

### Manual Tuning

If you prefer manual configuration, edit `/etc/postgresql/16/main/postgresql.conf`:

```ini
# Memory Configuration (2GB VPS)
shared_buffers = 512MB              # 25% of RAM
effective_cache_size = 1536MB       # 75% of RAM
work_mem = 16MB                     # For sorts/joins
maintenance_work_mem = 128MB        # For VACUUM/INDEX

# Connection Settings
max_connections = 100               # Align with asyncpg pool max_size

# Query Optimizer
random_page_cost = 1.1              # SSD-optimized (default: 4.0 for HDD)

# Write-Ahead Log (WAL)
wal_buffers = 16MB
checkpoint_completion_target = 0.9  # Spread checkpoints over longer time

# Logging (Slow Query Tracking)
logging_collector = on
log_min_duration_statement = 1000   # Log queries > 1 second
```

Restart PostgreSQL:
```bash
sudo systemctl restart postgresql
```

### Verify Settings

```bash
# Check memory settings
sudo -u postgres psql -c "SHOW shared_buffers; SHOW effective_cache_size; SHOW work_mem; SHOW maintenance_work_mem;" engagic

# Check active connections
sudo -u postgres psql -c "SELECT count(*) FROM pg_stat_activity;" engagic

# Monitor slow queries
sudo tail -f /var/log/postgresql/postgresql-16-main.log
```

### Connection Pool Sizing

Engagic uses asyncpg with configurable connection pooling. For 2GB VPS:

```bash
# Recommended pool settings (in .env or environment)
ENGAGIC_POSTGRES_POOL_MIN_SIZE=5    # Minimum idle connections
ENGAGIC_POSTGRES_POOL_MAX_SIZE=20   # Maximum concurrent connections
```

**Resource calculation:**
- 20 connections × 15MB avg = 300MB max
- Leaves ~1.7GB for OS, Python, and queries
- Safe for 2GB VPS with API + background daemon

**Monitoring:**
```bash
# Watch connection count in real-time
watch -n 2 "sudo -u postgres psql -c 'SELECT count(*), state FROM pg_stat_activity GROUP BY state;' engagic"
```

### Advanced: Enable pg_stat_statements

Track query performance with PostgreSQL's statistics extension:

```bash
# Enable extension
sudo -u postgres psql engagic -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"

# View top 10 slowest queries
sudo -u postgres psql engagic -c "
SELECT
    calls,
    total_exec_time::int,
    mean_exec_time::int,
    query
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
LIMIT 10;
"
```

---

## Migration from SQLite

See `MIGRATION.md` for SQLite → PostgreSQL data migration steps.
