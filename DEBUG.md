# Debugging Guide for Docker Compose

This guide explains how to debug your Docker Compose services in Cursor.

## 🐛 Debugging Methods

### Method 1: Remote Debugging (Recommended)

This method allows you to set breakpoints and debug code running inside Docker containers.

#### Step 1: Start services with debug enabled

```bash
# Start with debug configuration
docker-compose -f docker-compose.yml -f docker-compose.debug.yml up
```

This will:
- Start all services with `debugpy` listening on ports 5678 (API), 5679 (Scheduler), 5680 (Worker)
- Keep your code mounted as volumes (changes reflect immediately)

#### Step 2: Attach debugger in Cursor

1. **Set breakpoints** in your code (click left of line numbers)
2. **Open Run and Debug panel** (Ctrl+Shift+D / Cmd+Shift+D)
3. **Select debug configuration**:
   - `Python: Remote Attach (API)` - for debugging API service
   - `Python: Remote Attach (Scheduler)` - for debugging scheduler
   - `Python: Remote Attach (Worker)` - for debugging worker
4. **Click the green play button** or press F5
5. **Trigger your code** (make API request, wait for scheduler cycle, etc.)

#### Step 3: Debug!

- **Breakpoints** will pause execution
- **Variables** panel shows current values
- **Call Stack** shows execution path
- **Debug Console** allows Python expressions
- **Step Over (F10)**, **Step Into (F11)**, **Continue (F5)**

---

### Method 2: View Logs

Quick way to see what's happening without breakpoints:

```bash
# View all logs
docker-compose logs -f

# View specific service logs
docker-compose logs -f api
docker-compose logs -f scheduler
docker-compose logs -f worker

# View last 100 lines
docker-compose logs --tail=100 api
```

---

### Method 3: Interactive Shell

Access containers directly:

```bash
# Get shell in API container
docker-compose exec api bash

# Get shell in scheduler container
docker-compose exec scheduler bash

# Run Python interactively
docker-compose exec api python
```

---

### Method 4: Local Debugging (Without Docker)

Debug locally while connecting to Docker services:

1. **Start only infrastructure** (Redis, MongoDB):
   ```bash
   docker-compose up redis mongo
   ```

2. **Use local debug configuration**:
   - Select `Python: FastAPI (Local)` from debug dropdown
   - Press F5
   - Your code runs locally but connects to Docker Redis/MongoDB

---

## 🔧 Debug Configurations Explained

### Remote Attach Configurations

These connect to running containers:

- **Port 5678** → API service (`src/main.py`)
- **Port 5679** → Scheduler service (`src/scheduler/main.py`)
- **Port 5680** → Worker service (`src/worker.py`)

### Local Configurations

- **Python: Current File** → Debug the currently open Python file
- **Python: FastAPI (Local)** → Run FastAPI locally (connects to Docker services)

---

## 📝 Tips & Best Practices

### 1. Hot Reload

Your `docker-compose.yml` already has `--reload` flag, so code changes auto-reload. However:
- **Breakpoints** may need debugger re-attachment after reload
- **For stable debugging**, temporarily remove `--reload` in debug mode

### 2. Debugging Async Code

- Set breakpoints in async functions
- Use **Call Stack** to navigate async execution
- Check **Variables** panel for async context

### 3. Debugging Background Tasks

For scheduler/worker background tasks:
- Set breakpoint **before** the task starts
- Use **Conditional breakpoints** (right-click breakpoint → Edit)
- Example: `source_id == "specific_id"`

### 4. Environment Variables

Debug configurations use `.env` file automatically. To override:
- Edit `.env` file
- Or set in `docker-compose.debug.yml` environment section

### 5. Database Debugging

Inspect MongoDB:
```bash
# Connect to MongoDB shell
docker-compose exec mongo mongosh newsagent

# View collections
show collections

# Query sources
db.sources.find().pretty()
```

---

## 🚨 Troubleshooting

### Debugger won't attach

1. **Check if debug ports are exposed**:
   ```bash
   docker-compose ps
   # Should show ports 5678, 5679, 5680
   ```

2. **Check if debugpy is installed**:
   ```bash
   docker-compose exec api pip list | grep debugpy
   ```

3. **Restart containers**:
   ```bash
   docker-compose -f docker-compose.yml -f docker-compose.debug.yml restart
   ```

### Breakpoints not hitting

1. **Verify path mappings** in `.vscode/launch.json`:
   ```json
   "pathMappings": [
       {
           "localRoot": "${workspaceFolder}",
           "remoteRoot": "/app"
       }
   ]
   ```

2. **Check if code is actually running** (add print statements)

3. **Ensure breakpoint is on executable line** (not comments/blank lines)

### Code changes not reflecting

1. **Check volumes are mounted**:
   ```bash
   docker-compose exec api ls -la /app/src
   ```

2. **Restart service**:
   ```bash
   docker-compose restart api
   ```

---

## 📚 Quick Reference

| Action | Command |
|-------|---------|
| Start with debug | `docker-compose -f docker-compose.yml -f docker-compose.debug.yml up` |
| View logs | `docker-compose logs -f api` |
| Shell access | `docker-compose exec api bash` |
| Restart service | `docker-compose restart api` |
| Stop all | `docker-compose down` |

---

## 🎯 Example Debugging Session

1. **Start debug mode**:
   ```bash
   docker-compose -f docker-compose.yml -f docker-compose.debug.yml up
   ```

2. **Set breakpoint** in `src/scheduler/main.py` line 44 (inside `check_single_source`)

3. **Attach debugger**: Select "Python: Remote Attach (Scheduler)" → F5

4. **Wait for scheduler cycle** (runs every 1 minute) or trigger manually via API

5. **Debug**: When breakpoint hits, inspect `source`, `url`, `found_urls` variables

6. **Step through**: Use F10 to step through the function

7. **Continue**: Press F5 to continue execution

---

Happy Debugging! 🐛🔍
