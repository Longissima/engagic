#!/bin/bash
# engagic deployment script - API and fetcher management
set -e

APP_DIR="/opt/engagic"
VENV_DIR="/opt/engagic/.venv"
API_SERVICE="engagic-api"
API_SERVICE_FILE="/etc/systemd/system/${API_SERVICE}.service"
FETCHER_SERVICE="engagic-fetcher"
FETCHER_SERVICE_FILE="/etc/systemd/system/${FETCHER_SERVICE}.service"
PROCESSOR_SERVICE="engagic-processor"
MIGRATIONS_CHECKED=false
MIGRATION_FETCHER_WAS_ACTIVE=false

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log() {
    echo -e "${GREEN}[$(date '+%H:%M:%S')] $1${NC}"
}

warn() {
    echo -e "${YELLOW}[$(date '+%H:%M:%S')] $1${NC}"
}

error() {
    echo -e "${RED}[$(date '+%H:%M:%S')] ERROR: $1${NC}"
    exit 1
}

info() {
    echo -e "${BLUE}[$(date '+%H:%M:%S')] $1${NC}"
}

check_uv() {
    if ! command -v uv &> /dev/null; then
        log "Installing uv..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
        source "$HOME/.bashrc"
    fi
}

load_env() {
    cd "$APP_DIR"
    source "$VENV_DIR/bin/activate"
    # Source .env (PostgreSQL, etc.)
    if [ -f "$APP_DIR/.env" ]; then
        set -a
        source "$APP_DIR/.env"
        set +a
    fi
    # Source API keys (Gemini, etc.)
    if [ -f "$APP_DIR/.llm_secrets" ]; then
        set -a
        source "$APP_DIR/.llm_secrets"
        set +a
    fi
    # Use colored dev logs for interactive CLI (systemd services use JSON)
    export ENGAGIC_LOG_FORMAT=dev
}

list_migration_worker_blockers() {
    local include_fetcher="${1:-true}"
    local process_sessions

    process_sessions=$(list_process_sessions)
    if [ -n "$process_sessions" ]; then
        echo "$process_sessions" | sed 's/^/manual screen: /'
    fi
    if systemctl is-active --quiet "$PROCESSOR_SERVICE"; then
        echo "systemd service: $PROCESSOR_SERVICE"
    fi
    if [ "$include_fetcher" = true ] && systemctl is-active --quiet "$FETCHER_SERVICE"; then
        echo "systemd service: $FETCHER_SERVICE"
    fi
}

assert_migration_workers_quiescent() {
    local include_fetcher="${1:-true}"
    local blockers

    blockers=$(list_migration_worker_blockers "$include_fetcher")
    if [ -n "$blockers" ]; then
        warn "Database migrations deferred while pipeline workers are active:"
        echo "$blockers" | sed 's/^/  /'
        error "Let manual jobs finish and stop managed workers before migrating; detaching a screen does not drain its work"
    fi
}

quiesce_fetcher_for_migrations() {
    MIGRATION_FETCHER_WAS_ACTIVE=false
    if [ "$MIGRATIONS_CHECKED" = true ]; then
        return
    fi

    # Refuse before touching the managed fetcher when a manual or separately
    # managed processor worker would block the migration anyway.
    assert_migration_workers_quiescent false
    if systemctl is-active --quiet "$FETCHER_SERVICE"; then
        MIGRATION_FETCHER_WAS_ACTIVE=true
        log "Stopping fetcher before database migrations..."
        systemctl stop "$FETCHER_SERVICE"
        log "Fetcher quiesced for database migrations"
    fi
}

run_migrations() {
    # Every process surface expects the current schema. Keep migration
    # application at the deployment/start boundary so workers never claim a
    # job and only then discover that a required relation is missing.
    if [ "$MIGRATIONS_CHECKED" = true ]; then
        return
    fi

    # Queue ownership migrations are additive, but every claimant/publisher
    # must drain before schema ownership changes. Dead/completed screen entries
    # are deliberately ignored by list_process_sessions().
    assert_migration_workers_quiescent true

    log "Applying pending database migrations..."
    load_env
    uv run python -m database.migrate
    MIGRATIONS_CHECKED=true
}

setup_env() {
    log "Setting up Python environment with uv..."
    check_uv

    cd "$APP_DIR"

    # Create venv with uv if doesn't exist
    if [ ! -d "$VENV_DIR" ]; then
        uv venv "$VENV_DIR"
        log "Created virtual environment with uv"
    fi

    # Install dependencies with uv
    source "$VENV_DIR/bin/activate"
    uv sync
    log "Dependencies installed"
}

create_api_service() {
    log "Creating systemd service file for API..."

    cat > "$API_SERVICE_FILE" << EOF
[Unit]
Description=Engagic API Service
After=network.target
Wants=network.target

[Service]
Type=simple
User=engagic
Group=engagic
WorkingDirectory=$APP_DIR
ExecStart=$VENV_DIR/bin/uvicorn server.main:app --host 127.0.0.1 --port 8000 --no-access-log
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Environment
Environment=PYTHONPATH=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
EnvironmentFile=-/opt/engagic/.llm_secrets

# Resource limits
LimitNOFILE=65536

# Security
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    log "API systemd service file created"
}

create_fetcher_service() {
    log "Creating systemd service file for fetcher..."

    cat > "$FETCHER_SERVICE_FILE" << EOF
[Unit]
Description=Engagic Fetcher (Auto City Sync)
After=network.target
Wants=network.target

[Service]
Type=simple
User=engagic
Group=engagic
WorkingDirectory=$APP_DIR
ExecStart=$VENV_DIR/bin/uv run python -m pipeline.conductor fetcher
Restart=always
RestartSec=60
StandardOutput=journal
StandardError=journal

# Environment
Environment=PYTHONPATH=$APP_DIR
EnvironmentFile=-$APP_DIR/.env

# Resource limits
LimitNOFILE=65536

# Security
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    log "Fetcher systemd service file created"
}


# API Management
start_api_service() {
    if [ ! -f "$API_SERVICE_FILE" ]; then
        create_api_service
    fi

    log "Starting API service..."
    systemctl enable "$API_SERVICE"

    if systemctl is-active --quiet "$API_SERVICE"; then
        warn "API already running, restarting..."
        systemctl restart "$API_SERVICE"
    else
        systemctl start "$API_SERVICE"
    fi

    sleep 2
    if systemctl is-active --quiet "$API_SERVICE"; then
        log "API started successfully"
        log "API logs: journalctl -u $API_SERVICE -f"
    else
        error "Failed to start API"
    fi
}

start_api() {
    run_migrations
    start_api_service
}

stop_api() {
    if systemctl is-active --quiet "$API_SERVICE"; then
        log "Stopping API..."
        systemctl stop "$API_SERVICE"
        log "API stopped"
    else
        warn "API not running"
    fi
}

restart_api_service() {
    log "Restarting API..."
    systemctl restart "$API_SERVICE"
    sleep 2
    if systemctl is-active --quiet "$API_SERVICE"; then
        log "API restarted successfully"
    else
        error "Failed to restart API"
    fi
}

restart_api() {
    quiesce_fetcher_for_migrations
    local restore_fetcher="$MIGRATION_FETCHER_WAS_ACTIVE"

    run_migrations
    restart_api_service
    if [ "$restore_fetcher" = true ]; then
        start_fetcher_service
    fi
}

# Fetcher Management
start_fetcher_service() {
    if [ ! -f "$FETCHER_SERVICE_FILE" ]; then
        create_fetcher_service
    fi

    log "Starting fetcher service..."
    systemctl enable "$FETCHER_SERVICE"

    if systemctl is-active --quiet "$FETCHER_SERVICE"; then
        warn "Fetcher already running, restarting..."
        systemctl restart "$FETCHER_SERVICE"
    else
        systemctl start "$FETCHER_SERVICE"
    fi

    sleep 2
    if systemctl is-active --quiet "$FETCHER_SERVICE"; then
        log "Fetcher started successfully"
        log "Fetcher logs: journalctl -u $FETCHER_SERVICE -f"
    else
        error "Failed to start fetcher"
    fi
}

start_fetcher() {
    quiesce_fetcher_for_migrations
    run_migrations
    start_fetcher_service
}

stop_fetcher() {
    if systemctl is-active --quiet "$FETCHER_SERVICE"; then
        log "Stopping fetcher..."
        systemctl stop "$FETCHER_SERVICE"
        log "Fetcher stopped"
    else
        warn "Fetcher not running"
    fi
}

restart_fetcher_service() {
    # Create service file if doesn't exist
    if [ ! -f "$FETCHER_SERVICE_FILE" ]; then
        create_fetcher_service
    fi

    log "Restarting fetcher..."
    systemctl enable "$FETCHER_SERVICE" 2>/dev/null || true
    systemctl restart "$FETCHER_SERVICE"
    sleep 2
    if systemctl is-active --quiet "$FETCHER_SERVICE"; then
        log "Fetcher restarted successfully"
    else
        error "Failed to restart fetcher"
    fi
}

restart_fetcher() {
    quiesce_fetcher_for_migrations
    run_migrations
    restart_fetcher_service
}

# Background Process Management (Manual Commands Only)
kill_background_processes() {
    log "Terminating ALL engagic background processes..."

    local was_running=false
    # Broader pattern to catch ALL engagic processes:
    # - engagic-daemon, engagic-conductor, engagic-*
    # - pipeline.conductor, pipeline.processor, pipeline.fetcher, pipeline.analyzer
    local PATTERN="engagic-|pipeline\.|/opt/engagic.*python"

    # Check for ANY running processes
    if pgrep -f "$PATTERN" > /dev/null; then
        was_running=true
        warn "Found running engagic processes:"
        pgrep -fa "$PATTERN" | sed 's/^/  /'
        echo ""

        # Immediate SIGKILL - no graceful shutdown, user wants immediate termination
        warn "Sending SIGKILL (immediate termination)..."
        pkill -KILL -f "$PATTERN" 2>/dev/null || true
        sleep 1
    fi

    # Final verification
    if pgrep -f "$PATTERN" > /dev/null; then
        error "Failed to stop some processes:"
        pgrep -fa "$PATTERN" | sed 's/^/  /'
        echo ""
        warn "Retry with: pkill -9 -f 'engagic'"
    else
        if [ "$was_running" = true ]; then
            log "All engagic processes terminated"
        else
            info "No engagic processes were running"
        fi
    fi
}

# Combined operations
start_all() {
    quiesce_fetcher_for_migrations
    run_migrations
    start_api_service
    start_fetcher_service
}

stop_all() {
    stop_api
    stop_fetcher
    # Manual screen jobs are independent, resumable operator work. Service
    # management must never kill them implicitly; use kill-process (scoped)
    # or the explicit legacy `kill` command when that is truly intended.
    local sessions
    sessions=$(list_process_sessions)
    if [ -n "$sessions" ]; then
        warn "Manual process screens were left running:"
        echo "$sessions" | sed 's/^/  /'
    fi
}

restart_all() {
    quiesce_fetcher_for_migrations
    run_migrations
    restart_api_service
    restart_fetcher_service
}

status_all() {
    echo -e "${BLUE}=== Engagic Status ===${NC}"
    echo ""

    # API status
    echo -e "${BLUE}API Status:${NC}"
    if systemctl is-active --quiet "$API_SERVICE"; then
        echo -e "${GREEN}  Running${NC}"
        echo "  Logs: journalctl -u $API_SERVICE -f"
        echo "  Test: curl http://localhost:8000/"
        systemctl status "$API_SERVICE" --no-pager | grep -E "Active:|Main PID:" | sed 's/^/  /'
    else
        echo -e "${YELLOW}  Not running${NC}"
    fi

    echo ""

    # Fetcher status
    echo -e "${BLUE}Fetcher Status:${NC}"
    if systemctl is-active --quiet "$FETCHER_SERVICE"; then
        echo -e "${GREEN}  Running${NC}"
        echo "  Logs: journalctl -u $FETCHER_SERVICE -f"
        systemctl status "$FETCHER_SERVICE" --no-pager | grep -E "Active:|Main PID:" | sed 's/^/  /'
    else
        echo -e "${YELLOW}  Not running${NC}"
    fi

    echo ""

    # Processor visibility only. This script does not provision, start, stop,
    # or restart the independently managed processor service.
    echo -e "${BLUE}Processor Status:${NC}"
    if systemctl is-active --quiet "$PROCESSOR_SERVICE"; then
        echo -e "${GREEN}  Running${NC}"
        echo "  Logs: journalctl -u $PROCESSOR_SERVICE -f"
        systemctl status "$PROCESSOR_SERVICE" --no-pager | grep -E "Active:|Main PID:" | sed 's/^/  /'
    elif systemctl cat "$PROCESSOR_SERVICE" > /dev/null 2>&1; then
        echo -e "${YELLOW}  Not running${NC}"
    else
        echo -e "${YELLOW}  Not provisioned${NC}"
    fi

    echo ""

    # Manual screens are independent operator-owned jobs. Report exactly the
    # same live set used by the migration guard, without duplicating systemd
    # workers in this section.
    echo -e "${BLUE}Manual Process Screens:${NC}"
    local process_sessions
    process_sessions=$(list_process_sessions)
    if [ -n "$process_sessions" ]; then
        echo -e "${YELLOW}  Running:${NC}"
        echo "$process_sessions" | sed 's/^/    /'
    else
        echo -e "${GREEN}  None${NC}"
    fi

    echo ""

    # Database stats - only if API is running
    if systemctl is-active --quiet "$API_SERVICE"; then
        echo -e "${BLUE}Database Stats:${NC}"
        curl -s "http://localhost:8000/api/stats" | python3 -m json.tool 2>/dev/null | head -10 | sed 's/^/  /' || echo "  Unable to fetch stats"
    fi
}

test_services() {
    log "Testing API endpoints..."

    if ! command -v curl &> /dev/null; then
        error "curl is required for testing"
    fi

    # Check systemd status
    if ! systemctl is-active --quiet "$API_SERVICE"; then
        warn "API not running, skipping tests"
        return
    fi
    
    # Test root endpoint
    if curl -s "http://localhost:8000/" | grep -q "engagic API"; then
        log "✓ Root endpoint"
    else
        warn "✗ Root endpoint"
    fi
    
    # Test health endpoint
    if curl -s "http://localhost:8000/api/health" | grep -q "healthy\|status"; then
        log "✓ Health endpoint"
    else
        warn "✗ Health endpoint"
    fi
    
    # Test stats endpoint
    if curl -s "http://localhost:8000/api/stats" | grep -q "cities\|meetings"; then
        log "✓ Stats endpoint"
    else
        warn "✗ Stats endpoint"
    fi
    
    # Test search endpoint
    if curl -s -X POST "http://localhost:8000/api/search" \
        -H "Content-Type: application/json" \
        -d '{"query":"94301"}' | grep -q "success\|message"; then
        log "✓ Search endpoint"
    else
        warn "✗ Search endpoint"
    fi
    
    # Check fetcher
    if systemctl is-active --quiet "$FETCHER_SERVICE"; then
        log "✓ Fetcher service"
    else
        warn "✗ Fetcher service"
    fi

    log "Service tests complete"
}

quick_update() {
    log "Quick update..."
    cd "$APP_DIR"
    git pull
    source "$VENV_DIR/bin/activate"
    uv sync
    restart_all
    log "Update complete!"
}

sync_deps() {
    log "Syncing dependencies..."
    cd "$APP_DIR"
    source "$VENV_DIR/bin/activate"
    uv sync
    log "Dependencies synced!"
}

deploy_full() {
    info "Full deployment starting..."
    check_uv
    setup_env
    restart_all
    sleep 2
    test_services
    status_all
    info "Deployment complete!"
}

# Daemon-specific commands
daemon_status() {
    load_env
    uv run engagic-conductor status
}

# Expand a friendly sync/process target into what engagic-conductor expects.
#   schools / schooldistricts -> regenerate munis/school-districts.txt from the
#       DB (active type='school_district') and return @munis/school-districts.txt
#   knowns -> alias for processed
#   <name> matching munis/<name>.txt (processed, bay-area, ...) -> @munis/<name>.txt
#   everything else (bananas, state codes, comma lists, explicit @file) is untouched.
# Diagnostics go to stderr so the resolved target is the only thing on stdout.
resolve_targets() {
    local target="$1"
    case "$target" in
        *@*|*,*|*/*) echo "$target"; return ;;
    esac
    [ "$target" = "knowns" ] && target="processed"
    if [ "$target" = "schools" ] || [ "$target" = "schooldistricts" ]; then
        load_env >&2
        local out="$APP_DIR/munis/school-districts.txt"
        {
            echo "# Auto-generated by 'run.sh sync schools' -- active type='school_district' jurisdictions"
            echo "# Regenerated $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
            PGPASSWORD="$ENGAGIC_POSTGRES_PASSWORD" psql -h "${ENGAGIC_POSTGRES_HOST:-localhost}" \
                -U "${ENGAGIC_POSTGRES_USER:-engagic}" -d "${ENGAGIC_POSTGRES_DB:-engagic}" -tAc \
                "SELECT banana FROM jurisdictions WHERE type='school_district' AND status='active' ORDER BY banana"
        } > "$out"
        warn "Regenerated munis/school-districts.txt ($(grep -vc '^#' "$out") districts)" >&2
        echo "@munis/school-districts.txt"
        return
    fi
    if [ -f "$APP_DIR/munis/${target}.txt" ]; then
        echo "@munis/${target}.txt"
        return
    fi
    echo "$target"
}

sync() {
    if [ -z "$1" ]; then
        error "Targets required (banana, state code, @file, or a name like 'processed'/'schools')"
    fi

    local TARGETS
    TARGETS=$(resolve_targets "$1")
    log "Syncing: $TARGETS"
    load_env
    uv run engagic-conductor sync "$TARGETS"
}

# Screen sessions for manual process jobs. Several can run at once: the queue
# dequeues with FOR UPDATE SKIP LOCKED and `process <target>` scopes its claim
# (and batch drain) to that target's cities, so concurrent workers never
# double-claim. Each job gets a target-derived name with a numeric suffix on
# collision -- e.g. engagic-process-school-districts, engagic-process-GA-2.
PROCESS_SCREEN_PREFIX="engagic-process"

# Live session names matching the process prefix, one per line.
list_process_sessions() {
    screen -list 2>/dev/null \
        | grep -E '\((Attached|Detached)\)' \
        | grep -oE "[0-9]+\.${PROCESS_SCREEN_PREFIX}[^[:space:]]*" \
        | sed 's/^[0-9]*\.//'
}

# True if a screen session with exactly this name is live.
process_session_exists() {
    list_process_sessions | grep -qx "$1"
}

# Build a unique screen name from the resolved targets.
process_screen_name() {
    local slug
    slug=$(echo "$1" | sed -E 's#^@?[a-zA-Z0-9_]+/##; s/\.txt$//; s/[^a-zA-Z0-9]+/-/g; s/^-+//; s/-+$//')
    [ -z "$slug" ] && slug="job"
    slug="${slug:0:24}"
    local base="${PROCESS_SCREEN_PREFIX}-${slug}"
    local name="$base" n=2
    while process_session_exists "$name"; do
        name="${base}-${n}"
        n=$((n + 1))
    done
    echo "$name"
}

process() {
    if [ -z "$1" ]; then
        error "Targets required (banana, state code, @file, or a name like 'processed'/'schools')"
    fi

    local TARGETS
    TARGETS=$(resolve_targets "$1")

    local SCREEN_NAME
    SCREEN_NAME=$(process_screen_name "$TARGETS")

    log "Starting process in detachable screen session: $SCREEN_NAME"
    log "Targets: $TARGETS"
    echo ""
    info "Commands:"
    echo "  Ctrl-A D                       - Detach (process keeps running)"
    echo "  $0 attach $SCREEN_NAME         - Reattach to view logs"
    echo "  $0 kill-process $SCREEN_NAME   - Stop this job"
    echo "  $0 attach                      - List all running jobs"
    echo ""
    sleep 2

    load_env

    screen -dmS "$SCREEN_NAME" bash -c "
        cd $APP_DIR
        source $VENV_DIR/bin/activate
        if [ -f $APP_DIR/.env ]; then set -a; source $APP_DIR/.env; set +a; fi
        if [ -f $APP_DIR/.llm_secrets ]; then set -a; source $APP_DIR/.llm_secrets; set +a; fi
        export ENGAGIC_LOG_FORMAT=dev
        echo 'Starting process...'
        echo ''
        uv run engagic-conductor process '$TARGETS'
        echo ''
        echo 'Processing complete. Press Enter to close.'
        read
    "

    screen -r "$SCREEN_NAME"
}

attach_process() {
    local want="$1" sessions count
    sessions=$(list_process_sessions)

    if [ -z "$sessions" ]; then
        warn "No process session running"
        echo "Start one with: $0 process <targets>   (e.g. $0 process schools)"
        return
    fi

    if [ -n "$want" ]; then
        if process_session_exists "$want"; then
            log "Attaching to $want (Ctrl-A D to detach)..."
            screen -r "$want"
        else
            warn "No session named '$want'. Running sessions:"
            echo "$sessions" | sed 's/^/  /'
        fi
        return
    fi

    count=$(echo "$sessions" | grep -c .)
    if [ "$count" -eq 1 ]; then
        log "Attaching to $sessions (Ctrl-A D to detach)..."
        screen -r "$sessions"
    else
        warn "Multiple process sessions running:"
        echo "$sessions" | sed 's/^/  /'
        echo "Attach one with: $0 attach <name>"
    fi
}

kill_process() {
    local want="$1" sessions count
    sessions=$(list_process_sessions)

    if [ -z "$sessions" ]; then
        info "No process session running"
        return
    fi

    if [ "$want" = "all" ]; then
        warn "Killing ALL process sessions..."
        echo "$sessions" | while read -r s; do
            [ -n "$s" ] && screen -S "$s" -X quit
        done
        pkill -f "engagic-conductor process" 2>/dev/null || true
        log "All process sessions terminated"
        return
    fi

    if [ -n "$want" ]; then
        if process_session_exists "$want"; then
            warn "Killing $want..."
            # Quit just this session; its child conductor dies with it. No global
            # pkill here -- with concurrent jobs that would kill sibling sessions.
            screen -S "$want" -X quit
            log "$want terminated"
        else
            warn "No session named '$want'. Running sessions:"
            echo "$sessions" | sed 's/^/  /'
        fi
        return
    fi

    count=$(echo "$sessions" | grep -c .)
    if [ "$count" -eq 1 ]; then
        warn "Killing process session $sessions..."
        screen -S "$sessions" -X quit
        pkill -f "engagic-conductor process" 2>/dev/null || true
        log "Process session terminated"
    else
        warn "Multiple process sessions running:"
        echo "$sessions" | sed 's/^/  /'
        echo "Kill one with: $0 kill-process <name>   |   all: $0 kill-process all"
    fi
}

sync_and_process() {
    if [ -z "$1" ]; then
        error "Targets required (banana, state code, @file, or a name like 'processed'/'schools')"
    fi

    local TARGETS
    TARGETS=$(resolve_targets "$1")
    log "Syncing and processing: $TARGETS"
    load_env
    uv run engagic-conductor sync-and-process "$TARGETS"
}

process_unprocessed() {
    load_env
    uv run engagic-conductor full-sync
}

preview_queue() {
    load_env
    uv run engagic-conductor preview-queue "${1:-}"
}

preview_watchlist() {
    load_env
    uv run engagic-conductor preview-watchlist
}

users() {
    load_env
    echo -e "${BLUE}=== Users ===${NC}"
    PGPASSWORD="$ENGAGIC_POSTGRES_PASSWORD" psql -U "$ENGAGIC_POSTGRES_USER" -d "$ENGAGIC_POSTGRES_DB" -h localhost -c "
SELECT
    u.email,
    u.created_at::date as joined,
    COUNT(a.id) as alerts,
    STRING_AGG(DISTINCT elem::text, ', ') as watching_cities
FROM userland.users u
LEFT JOIN userland.alerts a ON u.id = a.user_id AND a.active = true
LEFT JOIN LATERAL jsonb_array_elements_text(a.cities) elem ON true
GROUP BY u.id, u.email, u.created_at
ORDER BY u.created_at DESC;
"
    echo ""
    echo -e "${BLUE}=== Keyword Alerts ===${NC}"
    PGPASSWORD="$ENGAGIC_POSTGRES_PASSWORD" psql -U "$ENGAGIC_POSTGRES_USER" -d "$ENGAGIC_POSTGRES_DB" -h localhost -c "
SELECT
    u.email,
    STRING_AGG(DISTINCT elem::text, ', ') as cities,
    a.criteria->>'keywords' as keywords
FROM userland.alerts a
JOIN userland.users u ON u.id = a.user_id
LEFT JOIN LATERAL jsonb_array_elements_text(a.cities) elem ON true
WHERE a.active = true
  AND jsonb_array_length(COALESCE(a.criteria->'keywords', '[]'::jsonb)) > 0
GROUP BY u.email, a.id, a.criteria
ORDER BY u.email;
"
    echo ""
    echo -e "${BLUE}=== City Requests WITH Email (priority) ===${NC}"
    PGPASSWORD="$ENGAGIC_POSTGRES_PASSWORD" psql -U "$ENGAGIC_POSTGRES_USER" -d "$ENGAGIC_POSTGRES_DB" -h localhost -c "
SELECT DISTINCT elem as requested_city, u.email, a.created_at::date as requested
FROM userland.alerts a
JOIN userland.users u ON a.user_id = u.id
CROSS JOIN LATERAL jsonb_array_elements_text(a.cities) elem
WHERE NOT EXISTS (SELECT 1 FROM jurisdictions j WHERE j.banana = elem)
ORDER BY requested DESC, elem;
"
    echo ""
    echo -e "${BLUE}=== City Requests (anonymous searches) ===${NC}"
    PGPASSWORD="$ENGAGIC_POSTGRES_PASSWORD" psql -U "$ENGAGIC_POSTGRES_USER" -d "$ENGAGIC_POSTGRES_DB" -h localhost -c "
SELECT
    city_banana as city,
    request_count as requests,
    status,
    last_requested::date as last_req
FROM userland.city_requests
ORDER BY request_count DESC, last_requested DESC;
"
    echo ""
    echo -e "${BLUE}=== Top Searched Cities (tracked) ===${NC}"
    PGPASSWORD="$ENGAGIC_POSTGRES_PASSWORD" psql -U "$ENGAGIC_POSTGRES_USER" -d "$ENGAGIC_POSTGRES_DB" -h localhost -c "
SELECT
    entity_id as city,
    COUNT(*) as searches,
    MIN(created_at)::date as first_search,
    MAX(created_at)::date as last_search
FROM userland.activity_log
WHERE action = 'search' AND entity_type = 'city'
GROUP BY entity_id
ORDER BY searches DESC
LIMIT 20;
"
    echo ""
    echo -e "${BLUE}=== Top Searched Cities (from logs) ===${NC}"
    journalctl -u engagic-api --no-pager --since "2024-01-01" 2>/dev/null \
        | grep -oP 'POST /api/search "\K[^"]+' \
        | sort | uniq -c | sort -rn | head -20
}

sync_watchlist() {
    load_env
    uv run engagic-conductor sync-watchlist
}

process_watchlist() {
    load_env
    uv run engagic-conductor process-watchlist
}

extract_text() {
    if [ -z "$1" ]; then
        error "Meeting ID required"
    fi

    load_env

    # Build command with Click syntax
    if [ -n "$2" ]; then
        log "Extracting text from meeting $1 to $2..."
        uv run engagic-conductor extract-text "$1" --output-file "$2"
    else
        log "Extracting text preview from meeting $1..."
        uv run engagic-conductor extract-text "$1"
    fi
}

preview_items() {
    if [ -z "$1" ]; then
        error "Meeting ID required"
    fi

    load_env

    # Build command with Click syntax
    if [ "$2" = "--extract" ]; then
        if [ -n "$3" ]; then
            log "Previewing items for $1 with text extraction to $3..."
            uv run engagic-conductor preview-items "$1" --extract-text --output-dir "$3"
        else
            log "Previewing items for $1 with text extraction..."
            uv run engagic-conductor preview-items "$1" --extract-text
        fi
    else
        log "Previewing items structure for $1..."
        uv run engagic-conductor preview-items "$1"
    fi
}

# Prometheus Commands
PROMETHEUS_SERVICE="prometheus"

cmd_start_prometheus() {
    log "Starting Prometheus..."
    sudo systemctl start $PROMETHEUS_SERVICE
    log "Prometheus started"
}

cmd_stop_prometheus() {
    log "Stopping Prometheus..."
    sudo systemctl stop $PROMETHEUS_SERVICE
    log "Prometheus stopped"
}

cmd_restart_prometheus() {
    log "Restarting Prometheus..."
    sudo systemctl restart $PROMETHEUS_SERVICE
    log "Prometheus restarted"
}

cmd_logs_prometheus() {
    log "Streaming Prometheus logs (Ctrl+C to exit)..."
    sudo journalctl -u $PROMETHEUS_SERVICE -f
}

cmd_status_prometheus() {
    sudo systemctl status $PROMETHEUS_SERVICE
}

# Testing Commands
cmd_test_emails() {
    log "Sending test emails to ibansadowski12@gmail.com..."
    load_env
    uv run userland/scripts/test_emails.py ibansadowski12@gmail.com
}

# Moderation Commands
cmd_moderate() {
    load_env
    if [ "$1" = "review" ]; then
        if [ -z "$2" ]; then
            echo "Enter deliberation ID to review:"
            read -r delib_id
            uv run scripts/moderate.py review "$delib_id"
        else
            uv run scripts/moderate.py review "$2"
        fi
    else
        uv run scripts/moderate.py list
    fi
}

# Map Commands
cmd_map_import() {
    log "Importing Census TIGER boundaries..."
    load_env
    uv run python scripts/import_census_boundaries.py --all
    log "Census import complete"
}

cmd_map_tiles() {
    log "Generating PMTiles..."
    load_env
    uv run python scripts/generate_tiles.py --all
    log "Tiles generated"
}

cmd_map_status() {
    load_env
    uv run python scripts/import_census_boundaries.py --status
}

cmd_map_all() {
    log "Running full map pipeline..."
    cmd_map_import
    cmd_map_tiles
    log "Map pipeline complete"
}

# Security Commands
cmd_security() {
    log "Security Status:"
    echo ""
    echo "Firewall (UFW):"
    ufw status | head -5
    echo ""
    echo "Fail2ban:"
    echo "  Status: $(systemctl is-active fail2ban)"
    fail2ban-client status 2>/dev/null | grep "Jail list" || echo "  (could not query jails)"
    echo ""
    echo "SSH:"
    echo "  Password auth: $(grep -E '^PasswordAuthentication' /etc/ssh/sshd_config.d/*.conf 2>/dev/null || echo 'default (check main config)')"
    echo ""
    echo "Backups:"
    local latest_backup=$(ls -t /opt/engagic/data/backups/engagic_*.sql.gz 2>/dev/null | head -1)
    if [ -n "$latest_backup" ]; then
        echo "  Latest: $latest_backup"
        echo "  Size: $(du -h "$latest_backup" | cut -f1)"
    else
        echo "  No backups found (will run at 3 AM daily)"
    fi
    echo "  Total backup space: $(du -sh /opt/engagic/data/backups 2>/dev/null | cut -f1)"
}

show_help() {
    echo "Engagic Deploy"
    echo ""
    echo "Services:"
    echo "  start, stop, restart      - All services"
    echo "  status                    - Show status"
    echo "  kill                      - Kill manual processes"
    echo ""
    echo "Individual (use 'start api' or 'start-api'):"
    echo "  start-api, stop-api, restart-api"
    echo "  start-fetch, stop-fetch, restart-fetch"
    echo ""
    echo "Logs (use 'logs api' or 'logs-api'):"
    echo "  logs-api, logs-fetch, logs"
    echo ""
    echo "Deployment:"
    echo "  setup                     - Install dependencies"
    echo "  update                    - Quick update (git pull + restart)"
    echo "  deploy                    - Full deployment"
    echo "  test                      - Test API endpoints"
    echo "  sync-deps                 - Sync Python dependencies (uv sync)"
    echo ""
    echo "Prometheus:"
    echo "  start-prometheus, stop-prometheus, restart-prometheus"
    echo "  logs-prometheus, status-prometheus"
    echo ""
    echo "Tools:"
    echo "  test-emails               - Send test emails"
    echo "  moderate [review ID]      - List/review pending comments"
    echo "  security                  - Show security posture"
    echo ""
    echo "Map/Coverage:"
    echo "  map-import                - Import Census TIGER boundaries"
    echo "  map-tiles                 - Generate PMTiles"
    echo "  map-status                - Show geometry coverage"
    echo "  map-all                   - Full map pipeline"
    echo ""
    echo "Data Operations:"
    echo "    sync TARGETS              - Fetch meetings. TARGETS is one or more of:"
    echo "                                  city banana (paloaltoCA), county banana (montereycountyCA),"
    echo "                                  state code (GA), @file path, comma-separated; OR a shortcut:"
    echo "                                    <name>   -> munis/<name>.txt   (e.g. processed, bay-area)"
    echo "                                    knowns   -> munis/processed.txt"
    echo "                                    schools  -> all active type='school_district' (from DB)"
    echo "    process TARGETS           - Process in a named screen (survives SSH disconnect; run several at once)."
    echo "    sync-and-process TARGETS  - Fetch + process. Same TARGETS as sync."
    echo "    attach [NAME]             - Reattach to a process session (lists them if NAME omitted and >1 running)"
    echo "    kill-process [NAME|all]   - Stop a process session (or 'all'; lists them if NAME omitted and >1 running)"
    echo "    process-unprocessed       - Process all unprocessed meetings in queue"
    echo ""
    echo "  Watchlist:"
    echo "    preview-watchlist              - Show cities users are watching"
    echo "    sync-watchlist                 - Fetch + process watchlist cities"
    echo "    process-watchlist              - Process queued jobs for watchlist cities"
    echo ""
    echo "  Preview & Inspection:"
    echo "    preview-queue [CITY]           - Show queued jobs (no processing)"
    echo "    extract-text MEETING_ID [FILE] - Extract PDF text (no LLM)"
    echo "    preview-items MEETING_ID       - Preview items structure"
    echo "    users                          - Show users, alerts, and city requests"
    echo ""
    echo "Examples:"
    echo "  $0 status                              # Check what's running"
    echo "  $0 sync paloaltoCA                     # Fetch single city"
    echo "  $0 sync processed                      # Fetch munis/processed.txt (shortcut)"
    echo "  $0 sync knowns                         # Alias for processed"
    echo "  $0 sync schools                        # Fetch all school districts (DB type)"
    echo "  $0 sync @munis/bay-area.txt            # Fetch region from file"
    echo "  $0 sync montereycountyCA               # Fetch county + all linked cities"
    echo "  $0 sync GA                             # Fetch every jurisdiction in Georgia"
    echo "  $0 process paloaltoCA                  # Process in screen"
    echo "  $0 process GA                          # Process every Georgia jurisdiction"
    echo "  $0 sync-and-process paloaltoCA         # Fetch + process"
    echo "  $0 users                               # Show user activity"
}

# Main command handling
# Support both "restart-api" and "restart api" syntax
COMMAND="${1:-help}"
if [ -n "$2" ] && [[ "$1" =~ ^(start|stop|restart|logs)$ ]] && [[ "$2" =~ ^(api|fetch)$ ]]; then
    COMMAND="$1-$2"
    # Shift arguments so $2 becomes $1, $3 becomes $2, etc.
    shift
fi

case "$COMMAND" in
    # Setup
    setup)     setup_env ;;

    # Service commands
    start)          start_all ;;
    stop)           stop_all ;;
    restart)        restart_all ;;
    start-api)      start_api ;;
    stop-api)       stop_api ;;
    restart-api)    restart_api ;;
    start-fetch)    start_fetcher ;;
    stop-fetch)     stop_fetcher ;;
    restart-fetch)  restart_fetcher ;;
    kill)           kill_background_processes ;;

    # Status and logs
    status)         status_all ;;
    logs-api)
        if systemctl is-active --quiet "$API_SERVICE"; then
            log "Streaming API logs (Ctrl+C to exit)..."
            journalctl -u engagic-api -n 200 -f --no-pager
        else
            warn "API not running via systemd"
            if [ -f "$APP_DIR/logs/api.log" ]; then
                log "Streaming API logs (Ctrl+C to exit)..."
                tail -f "$APP_DIR/logs/api.log"
            else
                error "No API logs found"
            fi
        fi
        ;;
    logs-fetch)
        if systemctl is-active --quiet "$FETCHER_SERVICE"; then
            log "Streaming fetcher logs (Ctrl+C to exit)..."
            journalctl -u engagic-fetcher -n 200 -f --no-pager
        else
            warn "Fetcher not running via systemd"
        fi
        ;;
    logs)
        log "Showing all engagic logs (Ctrl+C to exit)..."
        if [ -f "/opt/engagic/engagic.log" ]; then
            tail -n 200 -f /opt/engagic/engagic.log
        else
            warn "No log file found at /opt/engagic/engagic.log"
        fi
        ;;
    
    # Testing
    test)           test_services ;;

    # Deployment
    update)         quick_update ;;
    deploy)         deploy_full ;;
    sync-deps)      sync_deps ;;

    # Prometheus
    start-prometheus)   cmd_start_prometheus ;;
    stop-prometheus)    cmd_stop_prometheus ;;
    restart-prometheus) cmd_restart_prometheus ;;
    logs-prometheus)    cmd_logs_prometheus ;;
    status-prometheus)  cmd_status_prometheus ;;

    # Tools
    test-emails)        cmd_test_emails ;;
    moderate)           cmd_moderate "$2" "$3" ;;
    security)           cmd_security ;;

    # Map
    map-import)         cmd_map_import ;;
    map-tiles)          cmd_map_tiles ;;
    map-status)         cmd_map_status ;;
    map-all)            cmd_map_all ;;

    # Data operations
    sync)                sync "$2" ;;
    process)             process "$2" ;;
    sync-and-process)    sync_and_process "$2" ;;
    attach)              attach_process "$2" ;;
    kill-process)        kill_process "$2" ;;
    process-unprocessed) process_unprocessed ;;

    # Watchlist and users
    preview-watchlist)   preview_watchlist ;;
    sync-watchlist)      sync_watchlist ;;
    process-watchlist)   process_watchlist ;;
    users)               users ;;

    # Preview and inspection
    preview-queue)       preview_queue "$2" ;;
    extract-text)        extract_text "$2" "$3" ;;
    preview-items)       preview_items "$2" "$3" "$4" ;;

    # Help
    help|*)              show_help ;;
esac
