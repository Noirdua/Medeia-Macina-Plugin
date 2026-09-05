local mp = require 'mp'
local utils = require 'mp.utils'
local msg = require 'mp.msg'

local M = {}

local MEDEIA_LUA_VERSION = '2026-08-02.3'

-- Expose a tiny breadcrumb for debugging which script version is loaded.
pcall(mp.set_property, 'user-data/medeia-lua-version', MEDEIA_LUA_VERSION)

-- Track whether uosc is available so menu calls don't fail with
-- "Can't find script 'uosc' to send message to."
local _uosc_loaded = false

mp.register_script_message('uosc-version', function(_ver)
    _uosc_loaded = true
end)

local function _is_script_loaded(name)
    local ok, list = pcall(mp.get_property_native, 'script-list')
    if not ok or type(list) ~= 'table' then
        return false
    end
    for _, s in ipairs(list) do
        if type(s) == 'table' then
            local n = s.name or ''
            if n == name or tostring(n):match('^' .. name .. '%d*$') then
                return true
            end
        elseif type(s) == 'string' then
            local n = s
            if n == name or tostring(n):match('^' .. name .. '%d*$') then
                return true
            end
        end
    end
    return false
end

local LOAD_URL_MENU_TYPE = 'medios_load_url'

local DOWNLOAD_FORMAT_MENU_TYPE = 'medios_download_pick_format'
local DOWNLOAD_STORE_MENU_TYPE = 'medios_download_pick_store'
local SCREENSHOT_TAG_MENU_TYPE = 'medeia_screenshot_tags'
local SCREENSHOT_SAVE_JOB_TIMEOUT = 120
local SCREENSHOT_SAVE_JOB_POLL_INTERVAL = 0.75

-- Menu types for the command submenu and trim prompt
local CMD_MENU_TYPE = 'medios_cmd_menu'
local TRIM_PROMPT_MENU_TYPE = 'medios_trim_prompt'
local SLEEP_PROMPT_MENU_TYPE = 'medeia_sleep_timer_prompt'
local _sleep_timer = nil

local PIPELINE_REQ_PROP = 'user-data/medeia-pipeline-request'
local PIPELINE_RESP_PROP = 'user-data/medeia-pipeline-response'
local PIPELINE_READY_PROP = 'user-data/medeia-pipeline-ready'
local CURRENT_WEB_URL_PROP = 'user-data/medeia-current-web-url'
local _pipeline_progress_ui = {
    overlay = nil,
    hide_token = 0,
    title = '',
    summary = '',
    detail = '',
}

function _pipeline_progress_ui.trim(text)
    return tostring(text or ''):gsub('^%s+', ''):gsub('%s+$', '')
end

function _pipeline_progress_ui.ass_escape(text)
    text = tostring(text or '')
    text = text:gsub('\\', '\\\\')
    text = text:gsub('{', '\\{')
    text = text:gsub('}', '\\}')
    text = text:gsub('\n', '\\N')
    return text
end

function _pipeline_progress_ui.truncate(text, max_len)
    text = _pipeline_progress_ui.trim(text)
    max_len = tonumber(max_len or 0) or 0
    if max_len <= 0 or #text <= max_len then
        return text
    end
    if max_len <= 3 then
        return text:sub(1, max_len)
    end
    return text:sub(1, max_len - 3) .. '...'
end

function _pipeline_progress_ui.kind_title(kind)
    kind = _pipeline_progress_ui.trim(kind):lower()
    if kind == 'mpv-download' then
        return 'Download'
    end
    if kind == 'mpv-screenshot' then
        return 'Screenshot'
    end
    return 'Pipeline'
end

function _pipeline_progress_ui.ensure_overlay()
    if _pipeline_progress_ui.overlay then
        return _pipeline_progress_ui.overlay
    end
    local ok, overlay = pcall(mp.create_osd_overlay, 'ass-events')
    if ok and overlay then
        _pipeline_progress_ui.overlay = overlay
    end
    return _pipeline_progress_ui.overlay
end

function _pipeline_progress_ui.cancel_hide()
    _pipeline_progress_ui.hide_token = (_pipeline_progress_ui.hide_token or 0) + 1
end

function _pipeline_progress_ui.render()
    local overlay = _pipeline_progress_ui.ensure_overlay()
    if not overlay then
        return
    end

    local width, height = 1280, 720
    local ok, w, h = pcall(mp.get_osd_size)
    if ok and tonumber(w or 0) and tonumber(h or 0) and w > 0 and h > 0 then
        width = math.floor(w)
        height = math.floor(h)
    end

    overlay.res_x = width
    overlay.res_y = height

    if _pipeline_progress_ui.summary == '' and _pipeline_progress_ui.detail == '' then
        overlay.data = ''
        overlay:update()
        return
    end

    local title = _pipeline_progress_ui.truncate(_pipeline_progress_ui.title ~= '' and _pipeline_progress_ui.title or 'Pipeline', 42)
    local summary = _pipeline_progress_ui.truncate(_pipeline_progress_ui.summary, 72)
    local detail = _pipeline_progress_ui.truncate(_pipeline_progress_ui.detail, 88)
    local lines = {
        '{\\b1}' .. _pipeline_progress_ui.ass_escape(title) .. '{\\b0}',
    }
    if summary ~= '' then
        lines[#lines + 1] = _pipeline_progress_ui.ass_escape(summary)
    end
    if detail ~= '' then
        lines[#lines + 1] = '{\\fs18\\c&HDDDDDD&}' .. _pipeline_progress_ui.ass_escape(detail) .. '{\\r}'
    end

    overlay.data = string.format(
        '{\\an9\\pos(%d,%d)\\fs22\\bord2\\shad1\\1c&HFFFFFF&\\3c&H111111&\\4c&H000000&}%s',
        width - 28,
        34,
        table.concat(lines, '\\N')
    )
    overlay:update()
end

function _pipeline_progress_ui.hide()
    _pipeline_progress_ui.cancel_hide()
    _pipeline_progress_ui.title = ''
    _pipeline_progress_ui.summary = ''
    _pipeline_progress_ui.detail = ''
    _pipeline_progress_ui.render()
end

function _pipeline_progress_ui.schedule_hide(delay_seconds)
    _pipeline_progress_ui.cancel_hide()
    local delay = tonumber(delay_seconds or 0) or 0
    if delay <= 0 then
        _pipeline_progress_ui.hide()
        return
    end
    local token = _pipeline_progress_ui.hide_token
    mp.add_timeout(delay, function()
        if token ~= _pipeline_progress_ui.hide_token then
            return
        end
        _pipeline_progress_ui.hide()
    end)
end

function _pipeline_progress_ui.update(title, summary, detail)
    _pipeline_progress_ui.cancel_hide()
    _pipeline_progress_ui.title = _pipeline_progress_ui.trim(title)
    _pipeline_progress_ui.summary = _pipeline_progress_ui.trim(summary)
    _pipeline_progress_ui.detail = _pipeline_progress_ui.trim(detail)
    _pipeline_progress_ui.render()
end

local function _get_lua_source_path()
    local info = nil
    pcall(function()
        info = debug.getinfo(1, 'S')
    end)
    local source = info and info.source or ''
    if type(source) == 'string' and source:sub(1, 1) == '@' then
        return source:sub(2)
    end
    return ''
end

local function _detect_repo_root()
    local function find_up(start_dir, relative_path, max_levels)
        local d = start_dir
        local levels = max_levels or 8
        for _ = 0, levels do
            if d and d ~= '' then
                local candidate = d .. '/' .. relative_path
                if utils.file_info(candidate) then
                    return candidate
                end
            end
            local parent = d and d:match('(.*)[/\\]') or nil
            if not parent or parent == d or parent == '' then
                break
            end
            d = parent
        end
        return nil
    end

    local bases = {
        (_get_lua_source_path():match('(.*)[/\\]') or ''),
        mp.get_script_directory() or '',
        utils.getcwd() or '',
        ((opts and opts.cli_path) and tostring(opts.cli_path):match('(.*)[/\\]') or ''),
    }

    for _, base in ipairs(bases) do
        if base ~= '' then
            local cli = find_up(base, 'CLI.py', 8)
            if cli and cli ~= '' then
                return cli:match('(.*)[/\\]') or ''
            end
        end
    end

    return ''
end

pcall(mp.set_property, 'user-data/medeia-lua-script', _get_lua_source_path())

local function _append_lua_log_file(payload)
    payload = tostring(payload or '')
    payload = payload:gsub('^%s+', ''):gsub('%s+$', '')
    if payload == '' then
        return nil
    end

    local log_dir = ''
    local repo_root = _detect_repo_root()
    if repo_root ~= '' then
        log_dir = utils.join_path(repo_root, 'Log')
    end
    if log_dir == '' then
        log_dir = os.getenv('TEMP') or os.getenv('TMP') or utils.getcwd() or ''
    end
    if log_dir == '' then
        return nil
    end

    local path = utils.join_path(log_dir, 'medeia-mpv-lua.log')
    local fh = io.open(path, 'a')
    if not fh and repo_root ~= '' then
        local tmp = os.getenv('TEMP') or os.getenv('TMP') or ''
        if tmp ~= '' then
            path = utils.join_path(tmp, 'medeia-mpv-lua.log')
            fh = io.open(path, 'a')
        end
    end
    if not fh then
        return nil
    end

    fh:write('[' .. tostring(os.date('%Y-%m-%d %H:%M:%S')) .. '] ' .. payload .. '\n')
    fh:close()
    return path
end

local function _emit_to_mpv_log(payload)
    payload = tostring(payload or '')
    if payload == '' then
        return
    end

    local text = '[medeia] ' .. payload
    local lower = payload:lower()
    if lower:find('[error]', 1, true)
        or lower:find('helper not running', 1, true)
        or lower:find('failed', 1, true)
        or lower:find('timeout', 1, true) then
        pcall(msg.error, text)
        return
    end
    if lower:find('[warn]', 1, true) or lower:find('warning', 1, true) then
        pcall(msg.warn, text)
        return
    end
    pcall(msg.verbose, text)
end

-- Dedicated Lua log: write directly to logs.db database for unified logging
-- Fallback to stderr if database unavailable
local function _lua_log(text)
    local payload = (text and tostring(text) or '')
    if payload == '' then
        return
    end

    _append_lua_log_file(payload)
    _emit_to_mpv_log(payload)

    -- Attempt to find repo root for database access
    local repo_root = _detect_repo_root()

    -- Write to logs.db via Python subprocess (non-blocking, async)
    if repo_root ~= '' then
        local python = (opts and opts.python_path) and tostring(opts.python_path) or 'python'
        local db_path = (repo_root .. '/logs.db'):gsub('\\', '/')
        local msg = payload:gsub('\\', '\\\\'):gsub("'", "\\'")

        local script = string.format(
            "import sqlite3; p='%s'; c=sqlite3.connect(p); c.execute(\"CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, level TEXT, module TEXT, message TEXT)\"); c.execute(\"INSERT INTO logs (level,module,message) VALUES (?,?,?)\", ('DEBUG','mpv','%s')); c.commit(); c.close()",
            db_path,
            msg
        )

        pcall(function()
            mp.command_native_async({ name = 'subprocess', args = { python, '-c', script } }, function() end)
        end)
    end
end

_lua_log('medeia lua loaded version=' .. tostring(MEDEIA_LUA_VERSION) .. ' script=' .. tostring(mp.get_script_name()))

-- Combined log: to database (primary) + _lua_log (which also writes to db)
local function _log_all(level, text)
    if not text or text == '' then
        return
    end
    level = tostring(level or 'INFO'):upper()
    text = tostring(text)
    
    -- Log with level prefix via _lua_log (which writes to database)
    _lua_log('[' .. level .. '] ' .. text)
end

local function ensure_uosc_loaded()
    if _uosc_loaded or _is_script_loaded('uosc') then
        _uosc_loaded = true
        return true
    end

    local entry = nil
    pcall(function()
        entry = mp.find_config_file('scripts/uosc.lua')
    end)
    if not entry or entry == '' then
        _lua_log('uosc entry not found at scripts/uosc.lua under config-dir')
        return false
    end

    local ok = pcall(mp.commandv, 'load-script', entry)
    if ok then
        _lua_log('Loaded uosc from: ' .. tostring(entry))
    else
        _lua_log('Failed to load uosc from: ' .. tostring(entry))
    end

    -- uosc will broadcast uosc-version on load; also re-check script-list if available.
    if _is_script_loaded('uosc') then
        _uosc_loaded = true
        return true
    end
    return _uosc_loaded
end

M._disable_input_section = function(name, reason)
    local section = tostring(name or '')
    if section == '' then
        return
    end
    local ok, err = pcall(mp.commandv, 'disable-section', section)
    if not ok then
        _lua_log('ui: disable-section failed name=' .. section .. ' reason=' .. tostring(reason or 'unknown') .. ' err=' .. tostring(err))
    end
end

-- mpv.conf already sets cursor-autohide=1000; re-setting it to the same value
-- on every menu/file event churns the Windows cursor show/hide state and is
-- what caused the frozen/invisible OS cursor icon. Only touch it if changed.
function M._ensure_cursor_autohide_default()
    local ok, current = pcall(mp.get_property_number, 'cursor-autohide')
    if ok and current == 1000 then
        return
    end
    pcall(mp.set_property, 'cursor-autohide', '1000')
end

function M._sync_uosc_cursor(reason)
    local why = tostring(reason or 'unknown')
    M._disable_input_section('input_console', why)
    M._disable_input_section('input_forced_console', why)
    M._disable_input_section('image', why)
    M._ensure_cursor_autohide_default()
    if ensure_uosc_loaded() then
        pcall(mp.commandv, 'script-message-to', 'uosc', 'sync-cursor')
    end
end

function M._schedule_uosc_cursor_resync(reason)
    local why = tostring(reason or 'unknown')
    M._cursor_resync_generation = (M._cursor_resync_generation or 0) + 1
    local generation = M._cursor_resync_generation
    local delays = { 0.05, 0.35 }
    for _, delay in ipairs(delays) do
        mp.add_timeout(delay, function()
            if generation ~= M._cursor_resync_generation then
                return
            end
            local video_info = mp.get_property_native('current-tracks/video')
            if not (type(video_info) == 'table' and video_info.image == true) then
                M._sync_uosc_cursor(why .. '@' .. tostring(delay))
            end
        end)
    end
end

function M._close_uosc_menu_and_sync(menu_type, reason)
    local why = tostring(reason or 'unknown')
    if ensure_uosc_loaded() then
        if menu_type and menu_type ~= '' then
            pcall(mp.commandv, 'script-message-to', 'uosc', 'close-menu', menu_type)
        else
            pcall(mp.commandv, 'script-message-to', 'uosc', 'close-menu')
        end
    end
    M._sync_uosc_cursor(why)
    return true
end

M._reset_uosc_input_state = function(reason)
    local why = tostring(reason or 'unknown')
    M._disable_input_section('input_console', why)
    M._disable_input_section('input_forced_console', why)
    M._disable_input_section('image', why)
    M._ensure_cursor_autohide_default()
    if not ensure_uosc_loaded() then
        return false
    end
    pcall(mp.commandv, 'script-message-to', 'uosc', 'close-menu')
    M._sync_uosc_cursor(why)
    return true
end

M._open_uosc_menu = function(menu_data, reason)
    local why = tostring(reason or 'menu')
    if not ensure_uosc_loaded() then
        _lua_log('menu: uosc not available; cannot open-menu reason=' .. why)
        return false
    end
    M._reset_uosc_input_state(why .. ':pre-open')
    local payload = utils.format_json(menu_data or {})
    local ok, err = pcall(mp.commandv, 'script-message-to', 'uosc', 'open-menu', payload)
    if not ok then
        _lua_log('menu: open-menu failed reason=' .. why .. ' err=' .. tostring(err))
        return false
    end
    mp.add_timeout(0.03, function()
        M._ensure_cursor_autohide_default()
        if ensure_uosc_loaded() then
            pcall(mp.commandv, 'script-message-to', 'uosc', 'sync-cursor')
        end
    end)
    return true
end

local function write_temp_log(prefix, text)
    if not text or text == '' then
        return nil
    end

    local dir = ''
    -- Prefer repo-root Log/ for easier discovery.
    -- NOTE: Avoid spawning cmd.exe/sh just to mkdir on Windows/Linux; console flashes are
    -- highly undesirable. If the directory doesn't exist, we fall back to TEMP.
    do
        local function find_up(start_dir, relative_path, max_levels)
            local d = start_dir
            local levels = max_levels or 6
            for _ = 0, levels do
                if d and d ~= '' then
                    local candidate = d .. '/' .. relative_path
                    if utils.file_info(candidate) then
                        return candidate
                    end
                end
                local parent = d and d:match('(.*)[/\\]') or nil
                if not parent or parent == d or parent == '' then
                    break
                end
                d = parent
            end
            return nil
        end

        local base = mp.get_script_directory() or utils.getcwd() or ''
        if base ~= '' then
            local cli = find_up(base, 'CLI.py', 6)
            if cli and cli ~= '' then
                local parent = cli:match('(.*)[/\\]') or ''
                if parent ~= '' then
                    dir = utils.join_path(parent, 'Log')
                end
            end
        end
    end
    if dir == '' then
        dir = os.getenv('TEMP') or os.getenv('TMP') or utils.getcwd() or ''
    end
    if dir == '' then
        return nil
    end
    local name = (prefix or 'medeia-mpv') .. '-' .. tostring(math.floor(mp.get_time() * 1000)) .. '.log'
    local path = utils.join_path(dir, name)
    local fh = io.open(path, 'w')
    if not fh then
        -- If Log/ wasn't created (or is not writable), fall back to TEMP.
        local tmp = os.getenv('TEMP') or os.getenv('TMP') or ''
        if tmp ~= '' and tmp ~= dir then
            path = utils.join_path(tmp, name)
            fh = io.open(path, 'w')
        end
        if not fh then
            return nil
        end
    end
    fh:write(text)
    fh:close()
    return path
end

local function trim(s)
    return (s:gsub('^%s+', ''):gsub('%s+$', ''))
end

-- Lyrics overlay toggle
-- The Python helper (python -m plugins.mpv.lyric) will read this property via IPC.
local LYRIC_VISIBLE_PROP = "user-data/medeia-lyric-visible"

local function lyric_get_visible()
    local ok, v = pcall(mp.get_property_native, LYRIC_VISIBLE_PROP)
    if not ok or v == nil then
        return true
    end
    return v and true or false
end

local function lyric_set_visible(v)
    pcall(mp.set_property_native, LYRIC_VISIBLE_PROP, v and true or false)
end

local function lyric_toggle()
    local now = not lyric_get_visible()
    lyric_set_visible(now)
    mp.osd_message("Lyrics: " .. (now and "on" or "off"), 1)
end

-- Default to visible unless user overrides.
lyric_set_visible(true)

-- Configuration (global so _lua_log can see python_path early)
opts = {
    python_path = "python",
    cli_path = nil -- Will be auto-detected if nil
}

-- Read script options from script-opts/medeia.conf when available
pcall(function()
    local mpopts = require('mp.options')
    mpopts.read_options(opts, 'medeia')
end)

local function find_file_upwards(start_dir, relative_path, max_levels)
    local dir = start_dir
    local levels = max_levels or 6
    for _ = 0, levels do
        if dir and dir ~= "" then
            local candidate = dir .. "/" .. relative_path
            if utils.file_info(candidate) then
                return candidate
            end
        end
        local parent = dir and dir:match("(.*)[/\\]") or nil
        if not parent or parent == dir or parent == "" then
            break
        end
        dir = parent
    end
    return nil
end

local function _append_unique_path(out, seen, path)
    path = trim(tostring(path or ''))
    if path == '' then
        return
    end
    local key = path:gsub('\\', '/'):lower()
    if seen[key] then
        return
    end
    seen[key] = true
    out[#out + 1] = path
end

local function _path_exists(path)
    path = trim(tostring(path or ''))
    if path == '' then
        return false
    end
    return utils.file_info(path) ~= nil
end

local function _normalize_fs_path(path)
    path = trim(tostring(path or ''))
    path = path:gsub('^"+', ''):gsub('"+$', '')
    return trim(path)
end

local function _build_python_candidates(configured_python, prefer_no_console)
    local candidates = {}
    local seen = {}

    local function add(path)
        path = trim(tostring(path or ''))
        if path == '' then
            return
        end
        local key = path:gsub('\\', '/'):lower()
        if seen[key] then
            return
        end
        seen[key] = true
        candidates[#candidates + 1] = path
    end

    local repo_root = _detect_repo_root()
    if repo_root ~= '' then
        local sep = package and package.config and package.config:sub(1, 1) or '/'
        if sep == '\\' then
            if prefer_no_console then
                add(repo_root .. '/.venv/Scripts/pythonw.exe')
                add(repo_root .. '/venv/Scripts/pythonw.exe')
            end
            add(repo_root .. '/.venv/Scripts/python.exe')
            add(repo_root .. '/venv/Scripts/python.exe')
        else
            add(repo_root .. '/.venv/bin/python3')
            add(repo_root .. '/.venv/bin/python')
            add(repo_root .. '/venv/bin/python3')
            add(repo_root .. '/venv/bin/python')
        end
    end

    if _path_exists(configured_python) then
        if prefer_no_console then
            local sep = package and package.config and package.config:sub(1, 1) or '/'
            if sep == '\\' then
                local low = configured_python:lower()
                if low:sub(-10) == 'python.exe' then
                    local pythonw = configured_python:sub(1, #configured_python - 10) .. 'pythonw.exe'
                    if _path_exists(pythonw) then
                        add(pythonw)
                    end
                end
            end
        end
        add(configured_python)
    elseif configured_python ~= '' and configured_python ~= 'python' and configured_python ~= 'python.exe' then
        add(configured_python)
    end

    local sep = package and package.config and package.config:sub(1, 1) or '/'
    if sep == '\\' then
        add('python')
        add('py')
    else
        add('python3')
        add('python')
    end

    return candidates
end

local function _detect_format_probe_script()
    local repo_root = _detect_repo_root()
    if repo_root ~= '' then
        local direct = utils.join_path(repo_root, 'plugins/mpv/format_probe.py')
        if _path_exists(direct) then
            return direct
        end
        direct = utils.join_path(repo_root, 'MPV/format_probe.py')
        if _path_exists(direct) then
            return direct
        end
    end

    local candidates = {}
    local seen = {}
    local source_dir = _get_lua_source_path():match('(.*)[/\\]') or ''
    local script_dir = mp.get_script_directory() or ''
    local cwd = utils.getcwd() or ''
    _append_unique_path(candidates, seen, find_file_upwards(source_dir, 'plugins/mpv/format_probe.py', 8))
    _append_unique_path(candidates, seen, find_file_upwards(script_dir, 'plugins/mpv/format_probe.py', 8))
    _append_unique_path(candidates, seen, find_file_upwards(cwd, 'plugins/mpv/format_probe.py', 8))
    _append_unique_path(candidates, seen, find_file_upwards(source_dir, 'MPV/format_probe.py', 8))
    _append_unique_path(candidates, seen, find_file_upwards(script_dir, 'MPV/format_probe.py', 8))
    _append_unique_path(candidates, seen, find_file_upwards(cwd, 'MPV/format_probe.py', 8))

    for _, candidate in ipairs(candidates) do
        if _path_exists(candidate) then
            return candidate
        end
    end

    return ''
end

local function _describe_subprocess_result(result)
    if result == nil then
        return 'result=nil'
    end
    if type(result) ~= 'table' then
        return 'result=' .. tostring(result)
    end

    local parts = {}
    if result.error ~= nil then
        parts[#parts + 1] = 'error=' .. tostring(result.error)
    end
    if result.status ~= nil then
        parts[#parts + 1] = 'status=' .. tostring(result.status)
    end
    if result.killed_by_us ~= nil then
        parts[#parts + 1] = 'killed=' .. tostring(result.killed_by_us)
    end

    local stderr = trim(tostring(result.stderr or ''))
    if stderr ~= '' then
        stderr = stderr:gsub('[\r\n]+', ' ')
        if #stderr > 240 then
            stderr = stderr:sub(1, 240) .. '...'
        end
        parts[#parts + 1] = 'stderr=' .. stderr
    end

    local stdout = trim(tostring(result.stdout or ''))
    if stdout ~= '' then
        stdout = stdout:gsub('[\r\n]+', ' ')
        if #stdout > 160 then
            stdout = stdout:sub(1, 160) .. '...'
        end
        parts[#parts + 1] = 'stdout=' .. stdout
    end

    if #parts == 0 then
        return 'result={}'
    end
    return table.concat(parts, ', ')
end

local function _run_subprocess_command(cmd)
    local ok, result = pcall(mp.command_native, cmd)
    if not ok then
        return false, nil, tostring(result)
    end
    if type(result) == 'table' then
        local err = trim(tostring(result.error or ''))
        local status = tonumber(result.status)
        if (err ~= '' and err ~= 'success') or (status ~= nil and status ~= 0) then
            return false, result, _describe_subprocess_result(result)
        end
    end
    return true, result, _describe_subprocess_result(result)
end

local function _build_sibling_script_candidates(file_name)
    local candidates = {}
    local seen = {}
    local script_dir = mp.get_script_directory() or ''
    local cwd = utils.getcwd() or ''

    if script_dir ~= '' then
        _append_unique_path(candidates, seen, script_dir .. '/' .. file_name)
        _append_unique_path(candidates, seen, script_dir .. '/LUA/' .. file_name)
        _append_unique_path(candidates, seen, script_dir .. '/../' .. file_name)
        _append_unique_path(candidates, seen, find_file_upwards(script_dir, 'plugins/mpv/LUA/' .. file_name, 8))
        _append_unique_path(candidates, seen, find_file_upwards(script_dir, 'MPV/LUA/' .. file_name, 8))
    end

    if cwd ~= '' then
        _append_unique_path(candidates, seen, find_file_upwards(cwd, 'plugins/mpv/LUA/' .. file_name, 8))
        _append_unique_path(candidates, seen, find_file_upwards(cwd, 'MPV/LUA/' .. file_name, 8))
    end

    return candidates
end

local function _load_lua_chunk_from_candidates(label, file_name)
    local candidates = _build_sibling_script_candidates(file_name)
    local last_error = nil

    for _, candidate in ipairs(candidates) do
        local ok_load, chunk_or_err, load_err = pcall(loadfile, candidate)
        if ok_load and chunk_or_err then
            local ok_run, result_or_err = pcall(chunk_or_err)
            if ok_run then
                _lua_log(label .. ': loaded from ' .. candidate)
                return true, result_or_err, candidate
            end
            last_error = tostring(result_or_err or 'runtime error')
            _lua_log(label .. ': runtime error at ' .. candidate .. ' (' .. last_error .. ')')
        elseif ok_load then
            last_error = tostring(load_err or 'loadfile failed')
            _lua_log(label .. ': load failed at ' .. candidate .. ' (' .. last_error .. ')')
        else
            last_error = tostring(chunk_or_err or 'loadfile failed')
            _lua_log(label .. ': load failed at ' .. candidate .. ' (' .. last_error .. ')')
        end
    end

    _lua_log(label .. ': load failed; candidates=' .. tostring(#candidates) .. ' last_error=' .. tostring(last_error or 'not found'))
    return false, nil, nil, last_error
end

-- Forward declaration (defined later) used by helper auto-start.
local _resolve_python_exe
local _refresh_store_cache
local _uosc_open_list_picker
local _run_pipeline_detached
local _run_pipeline_background_job

local _cached_store_names = {}
local _store_cache_loaded = false
local _store_cache_retry_pending = false

-- Optional index into _cached_store_names (used by some older menu code paths).
-- If unset, callers should fall back to reading SELECTED_STORE_PROP.
local _selected_store_index = nil

local SELECTED_STORE_PROP = 'user-data/medeia-selected-store'
local STORE_PICKER_MENU_TYPE = 'medeia_store_picker'
local _selected_store_loaded = false
local _current_url_for_web_actions
local _store_status_hint_for_url
local _refresh_current_store_url_status
local _skip_next_store_check_url = ''
local _pick_folder_windows
M._ytdl_download_format_fallbacks = M._ytdl_download_format_fallbacks or {}

function M._load_store_choices_direct_async(cb)
    cb = cb or function() end

    local refresh_state = M._store_direct_refresh_state or { inflight = false, callbacks = {} }
    M._store_direct_refresh_state = refresh_state

    if refresh_state.inflight then
        table.insert(refresh_state.callbacks, cb)
        _lua_log('stores: direct config load join existing request')
        return
    end

    local python = _resolve_python_exe(false)
    if not python or python == '' then
        cb(nil, 'no python executable available')
        return
    end

    local repo_root = _detect_repo_root()
    if not repo_root or repo_root == '' then
        cb(nil, 'repo root not found')
        return
    end

    local function finish(resp, err)
        local callbacks = refresh_state.callbacks or {}
        refresh_state.callbacks = {}
        refresh_state.inflight = false
        for _, pending_cb in ipairs(callbacks) do
            pcall(pending_cb, resp, err)
        end
    end

    local bootstrap = table.concat({
        'import json, os, sys',
        'root = sys.argv[1]',
        'if root:',
        '    os.chdir(root)',
        '    sys.path.insert(0, root) if root not in sys.path else None',
        'from SYS.logger import set_thread_stream',
        'set_thread_stream(sys.stderr)',
        'from SYS.config import load_config',
        'from PluginCore.backend_registry import list_configured_backend_names',
        'config = load_config()',
        'choices = list_configured_backend_names(config) or []',
        'sys.stdout.write(json.dumps({"choices": choices}, ensure_ascii=False))',
    }, '\n')

    refresh_state.inflight = true
    refresh_state.callbacks = { cb }
    _lua_log('stores: spawning direct config scan python=' .. tostring(python) .. ' root=' .. tostring(repo_root))

    mp.command_native_async(
        {
            name = 'subprocess',
            args = { python, '-c', bootstrap, repo_root },
            capture_stdout = true,
            capture_stderr = true,
            playback_only = false,
        },
        function(success, result, err)
            if not success then
                finish(nil, tostring(err or 'subprocess failed'))
                return
            end

            if type(result) ~= 'table' then
                finish(nil, 'invalid subprocess result')
                return
            end

            local status = tonumber(result.status or 0) or 0
            local stdout = trim(tostring(result.stdout or ''))
            local stderr = trim(tostring(result.stderr or ''))
            if status ~= 0 then
                local detail = stderr
                if detail == '' then
                    detail = tostring(result.error or ('direct store scan exited with status ' .. tostring(status)))
                end
                finish(nil, detail)
                return
            end

            if stdout == '' then
                local detail = stderr
                if detail == '' then
                    detail = 'direct store scan returned no output'
                end
                finish(nil, detail)
                return
            end

            local ok, resp = pcall(utils.parse_json, stdout)
            if ok and type(resp) == 'string' then
                ok, resp = pcall(utils.parse_json, resp)
            end
            if not ok or type(resp) ~= 'table' then
                local stdout_preview = stdout
                if #stdout_preview > 200 then
                    stdout_preview = stdout_preview:sub(1, 200) .. '...'
                end
                local stderr_preview = stderr
                if #stderr_preview > 200 then
                    stderr_preview = stderr_preview:sub(1, 200) .. '...'
                end
                _lua_log('stores: direct config parse failed stdout=' .. tostring(stdout_preview) .. ' stderr=' .. tostring(stderr_preview))
                finish(nil, 'failed to parse direct store scan output')
                return
            end

            finish(resp, nil)
        end
    )
end

local function _normalize_store_name(store)
    store = trim(tostring(store or ''))
    store = store:gsub('^"', ''):gsub('"$', '')
    return trim(store)
end

function M._store_name_is_visible_in_mpv(store)
    store = _normalize_store_name(store)
    return store ~= '' and store:lower() ~= 'local'
end

function M._visible_store_names()
    local out = {}
    if type(_cached_store_names) ~= 'table' then
        return out
    end
    for _, name in ipairs(_cached_store_names) do
        name = _normalize_store_name(name)
        if M._store_name_is_visible_in_mpv(name) then
            out[#out + 1] = name
        end
    end
    return out
end

local function _is_cached_store_name(store)
    local needle = _normalize_store_name(store)
    if needle == '' then
        return false
    end
    if type(_cached_store_names) ~= 'table' then
        return false
    end
    for _, name in ipairs(_cached_store_names) do
        if _normalize_store_name(name) == needle then
            return true
        end
    end
    return false
end

local function _get_script_opts_dir()
    local dir = nil
    pcall(function()
        dir = mp.command_native({ 'expand-path', '~~/script-opts' })
    end)
    if type(dir) ~= 'string' or dir == '' then
        return nil
    end
    return dir
end

local function _get_selected_store_conf_path()
    local dir = _get_script_opts_dir()
    if not dir then
        return nil
    end
    return utils.join_path(dir, 'medeia.conf')
end

function M._get_selected_store_state_path()
    local dir = _get_script_opts_dir()
    if not dir then
        return nil
    end
    return utils.join_path(dir, 'medeia-selected-store.json')
end

function M._get_store_cache_path()
    local dir = _get_script_opts_dir()
    if not dir then
        return nil
    end
    return utils.join_path(dir, 'medeia-store-cache.json')
end

function M._load_store_names_from_disk()
    local path = M._get_store_cache_path()
    if not path then
        return nil
    end
    local fh = io.open(path, 'r')
    if not fh then
        return nil
    end
    local raw = fh:read('*a')
    fh:close()
    raw = trim(tostring(raw or ''))
    if raw == '' then
        return nil
    end
    local ok, payload = pcall(utils.parse_json, raw)
    if not ok or type(payload) ~= 'table' or type(payload.choices) ~= 'table' then
        return nil
    end
    local out = {}
    for _, value in ipairs(payload.choices) do
        local name = _normalize_store_name(value)
        if name ~= '' then
            out[#out + 1] = name
        end
    end
    return #out > 0 and out or nil
end

function M._save_store_names_to_disk(names)
    if type(names) ~= 'table' or #names == 0 then
        return false
    end
    local path = M._get_store_cache_path()
    if not path then
        return false
    end
    local fh = io.open(path, 'w')
    if not fh then
        return false
    end
    fh:write(utils.format_json({ choices = names }))
    fh:close()
    return true
end

function M._prime_store_cache_from_disk()
    if type(_cached_store_names) == 'table' and #_cached_store_names > 0 then
        return true
    end
    local names = M._load_store_names_from_disk()
    if type(names) ~= 'table' or #names == 0 then
        return false
    end
    _cached_store_names = names
    _store_cache_loaded = true
    _lua_log('stores: primed ' .. tostring(#names) .. ' stores from disk cache')
    return true
end

local function _load_selected_store_from_disk()
    local state_path = M._get_selected_store_state_path()
    if state_path then
        local fh = io.open(state_path, 'r')
        if fh then
            local raw = fh:read('*a')
            fh:close()
            raw = trim(tostring(raw or ''))
            if raw ~= '' then
                local ok, payload = pcall(utils.parse_json, raw)
                if ok and type(payload) == 'table' then
                    local value = _normalize_store_name(payload.store)
                    if value ~= '' then
                        return value
                    end
                end
            end
        end
    end

    local path = _get_selected_store_conf_path()
    if not path then
        return nil
    end
    local fh = io.open(path, 'r')
    if not fh then
        return nil
    end
    for line in fh:lines() do
        local s = trim(tostring(line or ''))
        if s ~= '' and s:sub(1, 1) ~= '#' and s:sub(1, 1) ~= ';' then
            local k, v = s:match('^([%w_%-]+)%s*=%s*(.*)$')
            if k and v and k:lower() == 'store' then
                fh:close()
                v = _normalize_store_name(v)
                return v ~= '' and v or nil
            end
        end
    end
    fh:close()
    return nil
end

local function _save_selected_store_to_disk(store)
    local path = M._get_selected_store_state_path()
    if not path then
        return false
    end
    local fh = io.open(path, 'w')
    if not fh then
        return false
    end
    fh:write(utils.format_json({ store = _normalize_store_name(store) }))
    fh:close()
    return true
end

local function _get_selected_store()
    local v = ''
    pcall(function()
        v = tostring(mp.get_property(SELECTED_STORE_PROP) or '')
    end)
    return _normalize_store_name(v)
end

local function _set_selected_store(store)
    store = _normalize_store_name(store)
    pcall(mp.set_property, SELECTED_STORE_PROP, store)
    pcall(_save_selected_store_to_disk, store)
end

local function _ensure_selected_store_loaded()
    if _selected_store_loaded then
        return
    end
    _selected_store_loaded = true
    local disk = nil
    pcall(function()
        disk = _load_selected_store_from_disk()
    end)
    disk = _normalize_store_name(disk)
    if disk ~= '' then
        pcall(mp.set_property, SELECTED_STORE_PROP, disk)
    end
    pcall(function()
        local legacy_path = _get_selected_store_conf_path()
        if not legacy_path then
            return
        end
        local fh = io.open(legacy_path, 'r')
        if not fh then
            return
        end
        local raw = fh:read('*a')
        fh:close()
        raw = tostring(raw or '')
        if raw == '' or not raw:lower():find('store%s*=') then
            return
        end

        local lines = {}
        for line in raw:gmatch('[^\r\n]+') do
            local s = trim(tostring(line or ''))
            local k = s:match('^([%w_%-]+)%s*=')
            if not (k and k:lower() == 'store') then
                lines[#lines + 1] = line
            end
        end

        local out = table.concat(lines, '\n')
        if out ~= '' then
            out = out .. '\n'
        end
        local writer = io.open(legacy_path, 'w')
        if not writer then
            return
        end
        writer:write(out)
        writer:close()
    end)
    pcall(M._prime_store_cache_from_disk)
end

local _pipeline_helper_started = false
local _last_ipc_error = ''
local _last_ipc_last_req_json = ''
local _last_ipc_last_resp_json = ''

-- Debounce helper start attempts (window in seconds).
-- Initialize below zero so the very first startup attempt is never rejected.
local _helper_start_debounce_ts = -1000
local HELPER_START_DEBOUNCE = 2.0

-- Track ready-heartbeat freshness so stale or non-timestamp values don't mask a stopped helper
local _helper_ready_last_value = ''
local _helper_ready_last_seen_ts = 0
local HELPER_READY_STALE_SECONDS = 120.0
M._lyric_helper_state = M._lyric_helper_state or { last_start_ts = -1000, debounce = 3.0 }
M._subtitle_autoselect_state = M._subtitle_autoselect_state or { serial = 0, deadline = 0 }

function M._normalize_mpv_user_data_text(value)
    local text = trim(tostring(value or ''))
    if text:sub(1, 1) == '"' and text:sub(-1) == '"' and #text >= 2 then
        text = text:sub(2, -2)
    end
    return trim(text)
end

local function _is_pipeline_helper_ready()
    local helper_version = mp.get_property('user-data/medeia-pipeline-helper-version')
    if helper_version == nil or helper_version == '' then
        helper_version = mp.get_property_native('user-data/medeia-pipeline-helper-version')
    end
    helper_version = M._normalize_mpv_user_data_text(helper_version)
    if helper_version ~= '' and helper_version ~= '2026-03-23.1' then
        return false
    end

    local ready = mp.get_property(PIPELINE_READY_PROP)
    if ready == nil or ready == '' then
        ready = mp.get_property_native(PIPELINE_READY_PROP)
    end
    if not ready then
        _helper_ready_last_value = ''
        _helper_ready_last_seen_ts = 0
        return false
    end
    local s = M._normalize_mpv_user_data_text(ready)
    if s == '' or s == '0' then
        _helper_ready_last_value = s
        _helper_ready_last_seen_ts = 0
        return false
    end

    local now = mp.get_time() or 0
    if s ~= _helper_ready_last_value then
        _helper_ready_last_value = s
        _helper_ready_last_seen_ts = now
    end

    -- Prefer timestamp heartbeats from modern helpers.
    local n = tonumber(s)
    if n and n > 1000000000 then
        local os_now = (os and os.time) and os.time() or nil
        if os_now then
            local age = os_now - n
            if age < 0 then
                age = 0
            end
            if age <= HELPER_READY_STALE_SECONDS then
                return true
            end
            return false
        end
    end

    -- Fall back only for non-timestamp values so stale helper timestamps from a
    -- previous session do not look fresh right after Lua reload.
    if helper_version ~= '2026-03-23.1' then
        return false
    end
    if _helper_ready_last_seen_ts > 0 and (now - _helper_ready_last_seen_ts) <= HELPER_READY_STALE_SECONDS then
        return true
    end

    return false
end

local function _helper_ready_diagnostics()
    local raw_ready = mp.get_property(PIPELINE_READY_PROP)
    if raw_ready == nil or raw_ready == '' then
        raw_ready = mp.get_property_native(PIPELINE_READY_PROP)
    end
    local raw_helper_version = mp.get_property('user-data/medeia-pipeline-helper-version')
    if raw_helper_version == nil or raw_helper_version == '' then
        raw_helper_version = mp.get_property_native('user-data/medeia-pipeline-helper-version')
    end
    local ready = M._normalize_mpv_user_data_text(raw_ready)
    local helper_version = M._normalize_mpv_user_data_text(raw_helper_version)
    local now = mp.get_time() or 0
    local age = 'n/a'
    if _helper_ready_last_seen_ts > 0 then
        age = string.format('%.2fs', math.max(0, now - _helper_ready_last_seen_ts))
    end
    local heartbeat_age = 'n/a'
    local ready_epoch = tonumber(ready)
    local os_now = (os and os.time) and os.time() or nil
    if ready_epoch and ready_epoch > 1000000000 and os_now then
        heartbeat_age = string.format('%.2fs', math.max(0, os_now - ready_epoch))
    end
    return 'ready=' .. tostring(ready or '')
        .. ' raw_ready=' .. tostring(raw_ready or '')
        .. ' helper_version=' .. tostring(helper_version or '')
        .. ' raw_helper_version=' .. tostring(raw_helper_version or '')
        .. ' required_version=2026-03-23.1'
        .. ' heartbeat_age=' .. tostring(heartbeat_age)
        .. ' last_value=' .. tostring(_helper_ready_last_value or '')
        .. ' last_seen_age=' .. tostring(age)
end

local function get_mpv_ipc_path()
    local ipc = mp.get_property('input-ipc-server')
    if ipc and ipc ~= '' then
        return ipc
    end
    -- Fallback: fixed pipe/socket name used by MPV/mpv_ipc.py
    local sep = package and package.config and package.config:sub(1, 1) or '/'
    if sep == '\\' then
        return '\\\\.\\pipe\\mpv-medios-macina'
    end
    return '/tmp/mpv-medios-macina.sock'
end

local function ensure_mpv_ipc_server()
    -- `.mpv -play` (Python) controls MPV via JSON IPC. If mpv was started
    -- without --input-ipc-server, make sure we set one so the running instance
    -- can be controlled (instead of Python spawning a separate mpv).
    local ipc = mp.get_property('input-ipc-server')
    if ipc and ipc ~= '' then
        return true
    end

    local desired = get_mpv_ipc_path()
    if not desired or desired == '' then
        return false
    end

    local ok = pcall(mp.set_property, 'input-ipc-server', desired)
    if not ok then
        return false
    end
    local now = mp.get_property('input-ipc-server')
    return (now and now ~= '') and true or false
end

local function attempt_start_pipeline_helper_async(callback)
    -- Async version: spawn helper without blocking UI. Calls callback(success) when done.
    callback = callback or function() end
    local helper_start_state = M._helper_start_state or { inflight = false, callbacks = {} }
    M._helper_start_state = helper_start_state

    local function finish(success)
        local callbacks = helper_start_state.callbacks or {}
        helper_start_state.callbacks = {}
        helper_start_state.inflight = false
        for _, cb in ipairs(callbacks) do
            pcall(cb, success)
        end
    end
    
    if _is_pipeline_helper_ready() then
        callback(true)
        return
    end

    if helper_start_state.inflight then
        table.insert(helper_start_state.callbacks, callback)
        _lua_log('attempt_start_pipeline_helper_async: join existing startup')
        return
    end
    
    -- Debounce: don't spawn multiple helpers in quick succession
    local now = mp.get_time()
    if _helper_start_debounce_ts > -1 and (now - _helper_start_debounce_ts) < HELPER_START_DEBOUNCE then
        _lua_log('attempt_start_pipeline_helper_async: debounced (recent attempt)')
        callback(false)
        return
    end
    _helper_start_debounce_ts = now
    helper_start_state.inflight = true
    helper_start_state.callbacks = { callback }

    -- Clear any stale ready heartbeat from an earlier helper instance before spawning.
    pcall(mp.set_property, PIPELINE_READY_PROP, '')
    pcall(mp.set_property, 'user-data/medeia-pipeline-helper-version', '')
    _helper_ready_last_value = ''
    _helper_ready_last_seen_ts = 0
    
    local python = _resolve_python_exe(true)
    if not python or python == '' then
        _lua_log('attempt_start_pipeline_helper_async: no python executable available')
        finish(false)
        return
    end

    local helper_script = ''
    local repo_root = _detect_repo_root()
    if repo_root ~= '' then
        local direct = utils.join_path(repo_root, 'plugins/mpv/pipeline_helper.py')
        if _path_exists(direct) then
            helper_script = direct
        end
        if helper_script == '' then
            direct = utils.join_path(repo_root, 'MPV/pipeline_helper.py')
            if _path_exists(direct) then
                helper_script = direct
            end
        end
    end
    if helper_script == '' then
        local candidates = {}
        local seen = {}
        local source_dir = _get_lua_source_path():match('(.*)[/\\]') or ''
        local script_dir = mp.get_script_directory() or ''
        local cwd = utils.getcwd() or ''
        _append_unique_path(candidates, seen, find_file_upwards(source_dir, 'plugins/mpv/pipeline_helper.py', 8))
        _append_unique_path(candidates, seen, find_file_upwards(script_dir, 'plugins/mpv/pipeline_helper.py', 8))
        _append_unique_path(candidates, seen, find_file_upwards(cwd, 'plugins/mpv/pipeline_helper.py', 8))
        _append_unique_path(candidates, seen, find_file_upwards(source_dir, 'MPV/pipeline_helper.py', 8))
        _append_unique_path(candidates, seen, find_file_upwards(script_dir, 'MPV/pipeline_helper.py', 8))
        _append_unique_path(candidates, seen, find_file_upwards(cwd, 'MPV/pipeline_helper.py', 8))
        for _, candidate in ipairs(candidates) do
            if _path_exists(candidate) then
                helper_script = candidate
                break
            end
        end
    end
    if helper_script == '' then
        _lua_log('attempt_start_pipeline_helper_async: pipeline_helper.py not found')
        finish(false)
        return
    end

    local launch_root = repo_root
    if launch_root == '' then
        launch_root = helper_script:match('(.*)[/\\]plugins[/\\]mpv[/\\]') or helper_script:match('(.*)[/\\]MPV[/\\]') or (helper_script:match('(.*)[/\\]') or '')
    end

    local bootstrap = table.concat({
        'import os, runpy, sys',
        'script = sys.argv[1]',
        'root = sys.argv[2]',
        'if root:',
        '    os.chdir(root)',
        '    sys.path.insert(0, root) if root not in sys.path else None',
        'sys.argv = [script] + sys.argv[3:]',
        'runpy.run_path(script, run_name="__main__")',
    }, '\n')
    local args = { python, '-c', bootstrap, helper_script, launch_root, '--ipc', get_mpv_ipc_path(), '--timeout', '30' }
    _lua_log('attempt_start_pipeline_helper_async: spawning helper python=' .. tostring(python) .. ' script=' .. tostring(helper_script) .. ' root=' .. tostring(launch_root))

    -- Spawn detached; don't wait for it here (async).
    local ok, result, detail = _run_subprocess_command({ name = 'subprocess', args = args, detach = true })
    _lua_log('attempt_start_pipeline_helper_async: detached spawn result ' .. tostring(detail or ''))
    if not ok then
        _lua_log('attempt_start_pipeline_helper_async: detached spawn failed, retrying blocking')
        ok, result, detail = _run_subprocess_command({ name = 'subprocess', args = args })
        _lua_log('attempt_start_pipeline_helper_async: blocking spawn result ' .. tostring(detail or ''))
    end
    
    if not ok then
        _lua_log('attempt_start_pipeline_helper_async: spawn failed final=' .. tostring(detail or _describe_subprocess_result(result)))
        finish(false)
        return
    end

    -- Wait for helper to become ready in background (non-blocking).
    -- The Python helper can spend up to 12s recovering a stale singleton lock
    -- before it even starts connecting to mpv IPC, so the Lua-side wait must be
    -- comfortably longer than that to avoid false startup failures.
    local deadline = mp.get_time() + 45.0
    local timer
    timer = mp.add_periodic_timer(0.1, function()
        if _is_pipeline_helper_ready() then
            timer:kill()
            _lua_log('attempt_start_pipeline_helper_async: helper ready')
            finish(true)
            return
        end
        if mp.get_time() >= deadline then
            timer:kill()
            _lua_log('attempt_start_pipeline_helper_async: timeout waiting for ready ' .. _helper_ready_diagnostics())
            -- Reset debounce so the next attempt is not immediate; gives the
            -- still-running Python helper time to die or acquire the lock.
            _helper_start_debounce_ts = mp.get_time()
            finish(false)
        end
    end)
end


local function ensure_pipeline_helper_running()
    -- Check if helper is already running (don't spawn from here).
    -- Auto-start is handled via explicit menu action only.
    return _is_pipeline_helper_ready()
end

function M._resolve_repo_script(relative_path)
    relative_path = trim(tostring(relative_path or ''))
    if relative_path == '' then
        return '', ''
    end

    local repo_root = _detect_repo_root()
    if repo_root ~= '' then
        local direct = utils.join_path(repo_root, relative_path)
        if _path_exists(direct) then
            return direct, repo_root
        end
    end

    local candidates = {}
    local seen = {}
    local source_dir = _get_lua_source_path():match('(.*)[/\\]') or ''
    local script_dir = mp.get_script_directory() or ''
    local cwd = utils.getcwd() or ''
    _append_unique_path(candidates, seen, find_file_upwards(source_dir, relative_path, 8))
    _append_unique_path(candidates, seen, find_file_upwards(script_dir, relative_path, 8))
    _append_unique_path(candidates, seen, find_file_upwards(cwd, relative_path, 8))

    for _, candidate in ipairs(candidates) do
        if _path_exists(candidate) then
            local launch_root = candidate:match('(.*)[/\\]plugins[/\\]mpv[/\\]') or candidate:match('(.*)[/\\]MPV[/\\]') or (candidate:match('(.*)[/\\]') or '')
            return candidate, launch_root
        end
    end

    return '', repo_root
end

function M._attempt_start_lyric_helper_async(reason)
    reason = trim(tostring(reason or 'startup'))
    local state = M._lyric_helper_state or { last_start_ts = -1000, debounce = 3.0, launch_inflight = false }
    M._lyric_helper_state = state

    if not ensure_mpv_ipc_server() then
        _lua_log('lyric-helper: missing mpv IPC server reason=' .. tostring(reason))
        return false
    end

    if state.launch_inflight then
        return false
    end

    local now = mp.get_time() or 0
    if (state.last_start_ts or -1000) > -1 and (now - (state.last_start_ts or -1000)) < (state.debounce or 3.0) then
        return false
    end
    state.last_start_ts = now

    local python = _resolve_python_exe(true)
    if not python or python == '' then
        python = _resolve_python_exe(false)
    end
    if not python or python == '' then
        _lua_log('lyric-helper: no python executable available reason=' .. tostring(reason))
        return false
    end

    local lyric_script, launch_root = M._resolve_repo_script('plugins/mpv/lyric.py')
    if lyric_script == '' then
        lyric_script, launch_root = M._resolve_repo_script('MPV/lyric.py')
    end
    if lyric_script == '' then
        _lua_log('lyric-helper: lyric.py not found reason=' .. tostring(reason))
        return false
    end

    local bootstrap = table.concat({
        'import os, runpy, sys',
        'script = sys.argv[1]',
        'root = sys.argv[2]',
        'if root:',
        '    os.chdir(root)',
        '    sys.path.insert(0, root) if root not in sys.path else None',
        'sys.argv = [script] + sys.argv[3:]',
        'runpy.run_path(script, run_name="__main__")',
    }, '\n')

    local args = { python, '-c', bootstrap, lyric_script, launch_root, '--ipc', get_mpv_ipc_path() }
    local lyric_log = ''
    if launch_root ~= '' then
        lyric_log = utils.join_path(launch_root, 'Log/medeia-mpv-lyric.log')
    end
    if lyric_log ~= '' then
        args[#args + 1] = '--log'
        args[#args + 1] = lyric_log
    end

    state.launch_inflight = true
    local ok_async, async_err = pcall(mp.command_native_async, {
        name = 'subprocess',
        args = args,
        detach = true,
    }, function(success, result, callback_err)
        state.launch_inflight = false
        if not success then
            _lua_log('lyric-helper: async spawn failed reason=' .. tostring(reason) .. ' detail=' .. tostring(callback_err or 'subprocess failed'))
            return
        end
        if type(result) == 'table' then
            local result_err = trim(tostring(result.error or ''))
            local status = tonumber(result.status)
            if (result_err ~= '' and result_err ~= 'success') or (status ~= nil and status ~= 0) then
                _lua_log('lyric-helper: async spawn failed reason=' .. tostring(reason) .. ' detail=' .. tostring(_describe_subprocess_result(result)))
                return
            end
        end
        _lua_log('lyric-helper: start requested reason=' .. tostring(reason) .. ' script=' .. tostring(lyric_script))
    end)
    if not ok_async then
        state.launch_inflight = false
        _lua_log('lyric-helper: async spawn failed reason=' .. tostring(reason) .. ' detail=' .. tostring(async_err))
        return false
    end
    return true
end

local _ipc_async_busy = false
local _ipc_async_queue = {}

local function _run_helper_request_async(req, timeout_seconds, cb)
    cb = cb or function() end

    if type(req) ~= 'table' then
        _lua_log('ipc-async: invalid request')
        cb(nil, 'invalid request')
        return
    end

    -- Assign id and label early for logging
    local id = tostring(req.id or '')
    if id == '' then
        id = tostring(math.floor(mp.get_time() * 1000)) .. '-' .. tostring(math.random(100000, 999999))
        req.id = id
    end

    local label = ''
    if req.op then
        label = 'op=' .. tostring(req.op)
    elseif req.pipeline then
        label = 'cmd=' .. tostring(req.pipeline)
    else
        label = '(unknown)'
    end

    _lua_log('ipc-async: queuing request id=' .. id .. ' ' .. label .. ' (busy=' .. tostring(_ipc_async_busy) .. ', queue_size=' .. tostring(#_ipc_async_queue) .. ')')
    if _ipc_async_busy then
        _ipc_async_queue[#_ipc_async_queue + 1] = { req = req, timeout = timeout_seconds, cb = cb }
        return
    end
    _ipc_async_busy = true

    local function done(resp, err)
        local err_text = err and tostring(err) or ''
        local quiet = type(req) == 'table' and req.quiet and true or false
        local is_timeout = err_text:find('timeout waiting response', 1, true) ~= nil
        local retry_count = type(req) == 'table' and tonumber(req._retry or 0) or 0
        local op_name = type(req) == 'table' and tostring(req.op or '') or ''
        local is_retryable = is_timeout and type(req) == 'table' and retry_count < 1
            and (op_name == 'ytdlp-formats' or op_name == 'run-background')

        if is_retryable then
            req._retry = retry_count + 1
            req.id = nil
            _ipc_async_busy = false
            _lua_log('ipc-async: timeout on ' .. tostring(op_name) .. '; restarting helper and retrying (attempt ' .. tostring(req._retry) .. ')')
            pcall(mp.set_property, PIPELINE_READY_PROP, '')
            attempt_start_pipeline_helper_async(function(success)
                if success then
                    _lua_log('ipc-async: helper restart succeeded; retrying ' .. tostring(op_name))
                else
                    _lua_log('ipc-async: helper restart failed; retrying anyway')
                end
            end)
            mp.add_timeout(0.3, function()
                _run_helper_request_async(req, timeout_seconds, cb)
            end)
            return
        end

        if err then
            if quiet then
                _lua_log('ipc-async: done id=' .. tostring(id) .. ' unavailable ' .. tostring(label))
            else
                _lua_log('ipc-async: done id=' .. tostring(id) .. ' ERROR: ' .. tostring(err))
            end
        else
            _lua_log('ipc-async: done id=' .. tostring(id) .. ' success=' .. tostring(resp and resp.success))
        end
        _ipc_async_busy = false
        cb(resp, err)

        if #_ipc_async_queue > 0 then
            local next_job = table.remove(_ipc_async_queue, 1)
            -- Schedule next job slightly later to let mpv deliver any pending events.
            mp.add_timeout(0.01, function()
                _run_helper_request_async(next_job.req, next_job.timeout, next_job.cb)
            end)
        end
    end

    if type(req) ~= 'table' then
        done(nil, 'invalid request')
        return
    end

    ensure_mpv_ipc_server()

    local function send_request_payload()
        _lua_log('ipc-async: send request id=' .. tostring(id) .. ' ' .. label)
        local req_json = utils.format_json(req)
        _last_ipc_last_req_json = req_json

        mp.set_property(PIPELINE_RESP_PROP, '')
        mp.set_property(PIPELINE_REQ_PROP, req_json)

        local deadline = mp.get_time() + (timeout_seconds or 5)
        local poll_timer
        poll_timer = mp.add_periodic_timer(0.05, function()
            if mp.get_time() >= deadline then
                poll_timer:kill()
                done(nil, 'timeout waiting response (' .. label .. ')')
                return
            end

            local resp_json = mp.get_property(PIPELINE_RESP_PROP)
            if resp_json and resp_json ~= '' then
                _last_ipc_last_resp_json = resp_json
                local ok, resp = pcall(utils.parse_json, resp_json)
                if ok and resp and resp.id == id then
                    poll_timer:kill()
                    _lua_log('ipc-async: got response id=' .. tostring(id) .. ' success=' .. tostring(resp.success))
                    done(resp, nil)
                end
            end
        end)
    end

    local function wait_for_helper_ready(timeout, on_ready)
        local deadline = mp.get_time() + (timeout or 3.0)
        local ready_timer
        ready_timer = mp.add_periodic_timer(0.05, function()
            if _is_pipeline_helper_ready() then
                ready_timer:kill()
                on_ready()
                return
            end
            if mp.get_time() >= deadline then
                ready_timer:kill()
                _lua_log('ipc-async: helper wait timed out ' .. _helper_ready_diagnostics())
                done(nil, 'helper not ready')
                return
            end
        end)
    end

    local function ensure_helper_and_send()
        if _is_pipeline_helper_ready() then
            wait_for_helper_ready(3.0, send_request_payload)
            return
        end

        _lua_log('ipc-async: helper not ready, auto-starting before request id=' .. id)
        attempt_start_pipeline_helper_async(function(success)
            if not success then
                _lua_log('ipc-async: helper auto-start failed while handling request id=' .. id)
            else
                _lua_log('ipc-async: helper auto-start triggered by request id=' .. id)
            end
        end)

        local helper_deadline = mp.get_time() + 6.0
        local helper_timer
        helper_timer = mp.add_periodic_timer(0.1, function()
            if _is_pipeline_helper_ready() then
                helper_timer:kill()
                wait_for_helper_ready(3.0, send_request_payload)
                return
            end
            if mp.get_time() >= helper_deadline then
                helper_timer:kill()
                _lua_log('ipc-async: helper still not running after auto-start ' .. _helper_ready_diagnostics())
                done(nil, 'helper not running')
                return
            end
        end)
    end

    ensure_helper_and_send()
end

local function run_pipeline_via_ipc_async(pipeline_cmd, seeds, timeout_seconds, cb)
    local req = { pipeline = pipeline_cmd }
    if seeds then
        req.seeds = seeds
    end
    _run_helper_request_async(req, timeout_seconds, function(resp, err)
        if type(cb) == 'function' then
            cb(resp, err)
        end
    end)
end

local function _url_can_direct_load(url)
    -- Determine if a URL is safe to load directly via mpv loadfile (vs. requiring pipeline).
    -- Complex streams like MPD/DASH manifests and ytdl URLs need the full pipeline.
    url = tostring(url or '');
    local lower = url:lower()
    
    -- File paths and simple URLs are OK
    if lower:match('^file://') or lower:match('^file:///') then return true end
    if not lower:match('^https?://') and not lower:match('^rtmp') then return true end
    
    -- Block ytdl and other complex streams
    if lower:match('youtube%.com') or lower:match('youtu%.be') then return false end
    if lower:match('%.mpd%b()') or lower:match('%.mpd$') then return false end  -- DASH manifest
    if lower:match('manifest%.json') then return false end
    if lower:match('twitch%.tv') or lower:match('youtube') then return false end
    if lower:match('soundcloud%.com') or lower:match('bandcamp%.com') then return false end
    if lower:match('spotify') or lower:match('tidal') then return false end
    if lower:match('reddit%.com') or lower:match('tiktok%.com') then return false end
    if lower:match('vimeo%.com') or lower:match('dailymotion%.com') then return false end
    
    -- Default: assume direct load is OK for plain HTTP(S) URLs
    return true
end

local function _try_direct_loadfile(url, force)
    -- Attempt to load URL directly via mpv without pipeline.
    -- Returns (success: bool, loaded: bool) where:
    -- - success=true, loaded=true: URL loaded successfully
    -- - success=true, loaded=false: URL not suitable for direct load (when not forced)
    -- - success=false: loadfile command failed
    force = force and true or false
    if not force or not _url_can_direct_load(url) then
        _lua_log('_try_direct_loadfile: skip loadfile force=' .. tostring(force) .. ' url=' .. tostring(url))
        return true, false
    end
    _lua_log('_try_direct_loadfile: attempting loadfile for ' .. url)
    local ok_load = pcall(mp.commandv, 'loadfile', url, 'replace')
    _lua_log('_try_direct_loadfile: loadfile result ok_load=' .. tostring(ok_load))
    return ok_load, ok_load  -- Fallback attempted
end

local function quote_pipeline_arg(s)
    -- Ensure URLs with special characters (e.g. &, #) survive pipeline parsing.
    s = tostring(s or '')
    s = s:gsub('\\', '\\\\'):gsub('"', '\\"')
    return '"' .. s .. '"'
end

local function _is_windows()
    local sep = package and package.config and package.config:sub(1, 1) or '/'
    return sep == '\\'
end

_resolve_python_exe = function(prefer_no_console)
    local configured = trim(tostring((opts and opts.python_path) or 'python'))
    local candidates = _build_python_candidates(configured, prefer_no_console)

    for _, candidate in ipairs(candidates) do
        if candidate:match('[/\\]') then
            if _path_exists(candidate) then
                return candidate
            end
        else
            return candidate
        end
    end

    return configured ~= '' and configured or 'python'
end

local function _extract_target_from_memory_uri(text)
    if type(text) ~= 'string' then
        return nil
    end
    if not text:match('^memory://') then
        return nil
    end
    for line in text:gmatch('[^\r\n]+') do
        line = trim(line)
        if line ~= '' and not line:match('^#') and not line:match('^memory://') then
            return line
        end
    end
    return nil
end

local function _percent_decode(s)
    if type(s) ~= 'string' then
        return s
    end
    return (s:gsub('%%(%x%x)', function(hex)
        return string.char(tonumber(hex, 16))
    end))
end

local function _extract_query_param(url, key)
    if type(url) ~= 'string' then
        return nil
    end
    key = tostring(key or '')
    if key == '' then
        return nil
    end
    local pattern = '[?&]' .. key:gsub('([^%w])', '%%%1') .. '=([^&#]+)'
    local v = url:match(pattern)
    if v then
        return _percent_decode(v)
    end
    return nil
end

local function _download_url_for_current_item(url)
    url = trim(tostring(url or ''))
    if url == '' then
        return '', false
    end

    local base, query = url:match('^([^?]+)%?(.*)$')
    if not base or not query or query == '' then
        return url, false
    end

    local base_lower = tostring(base or ''):lower()
    local has_explicit_video = false
    if base_lower:match('youtu%.be/') then
        has_explicit_video = true
    elseif base_lower:match('youtube%.com/watch') or base_lower:match('youtube%-nocookie%.com/watch') then
        has_explicit_video = _extract_query_param(url, 'v') ~= nil
    end

    if not has_explicit_video then
        return url, false
    end

    local kept = {}
    local changed = false
    for pair in query:gmatch('[^&]+') do
        local raw_key = pair:match('^([^=]+)') or pair
        local key = tostring(_percent_decode(raw_key) or raw_key or ''):lower()
        local keep = true
        if key == 'list' or key == 'index' or key == 'start_radio' or key == 'pp' or key == 'si' then
            keep = false
            changed = true
        end
        if keep then
            kept[#kept + 1] = pair
        end
    end

    if not changed then
        return url, false
    end
    if #kept > 0 then
        return base .. '?' .. table.concat(kept, '&'), true
    end
    return base, true
end

function M._extract_youtube_video_id(url)
    url = trim(tostring(url or ''))
    if url == '' then
        return nil
    end

    local lower = url:lower()
    local video_id = nil
    if lower:match('youtu%.be/') then
        video_id = url:match('youtu%.be/([^%?&#/]+)')
    elseif lower:match('youtube%.com/watch') or lower:match('youtube%-nocookie%.com/watch') then
        video_id = _extract_query_param(url, 'v')
    elseif lower:match('youtube%.com/shorts/') or lower:match('youtube%-nocookie%.com/shorts/') then
        video_id = url:match('/shorts/([^%?&#/]+)')
    elseif lower:match('youtube%.com/live/') or lower:match('youtube%-nocookie%.com/live/') then
        video_id = url:match('/live/([^%?&#/]+)')
    elseif lower:match('youtube%.com/embed/') or lower:match('youtube%-nocookie%.com/embed/') then
        video_id = url:match('/embed/([^%?&#/]+)')
    end

    video_id = trim(tostring(video_id or ''))
    if video_id == '' or not video_id:match('^[%w_-]+$') then
        return nil
    end
    return video_id
end

function M._suspicious_ytdl_format_reason(fmt, url, raw)
    fmt = trim(tostring(fmt or ''))
    url = trim(tostring(url or ''))
    if fmt == '' then
        return nil
    end

    local lower_fmt = fmt:lower()
    if lower_fmt:match('^https?://') or lower_fmt:match('^rtmp') or (url ~= '' and fmt == url) then
        return 'format string is a url'
    end

    local youtube_id = M._extract_youtube_video_id(url)
    if youtube_id and fmt == youtube_id then
        return 'format matches current youtube video id'
    end

    if type(raw) == 'table' and youtube_id then
        local raw_id = trim(tostring(raw.id or ''))
        if raw_id ~= '' and raw_id == youtube_id and fmt == raw_id then
            return 'format matches raw youtube video id'
        end
    end

    if fmt:match('^%d+%-%d+$') and type(raw) == 'table' and type(raw.formats) == 'table' then
        for _, item in ipairs(raw.formats) do
            if type(item) == 'table' and trim(tostring(item.format_id or '')) == fmt then
                local protocol = trim(tostring(item.protocol or '')):lower()
                local size_bytes = item.filesize or item.filesize_approx
                local vcodec = tostring(item.vcodec or 'none')
                local acodec = tostring(item.acodec or 'none')
                if (protocol == 'm3u8' or protocol == 'm3u8_native')
                    and not size_bytes
                    and vcodec ~= 'none'
                    and acodec ~= 'none' then
                    return 'format is transient hls variant selector'
                end
                break
            end
        end
    end

    if fmt:match('^%d+%-%w+$') and type(raw) == 'table' and type(raw.formats) == 'table' then
        for _, item in ipairs(raw.formats) do
            if type(item) == 'table' and trim(tostring(item.format_id or '')) == fmt then
                local vcodec = tostring(item.vcodec or 'none')
                local acodec = tostring(item.acodec or 'none')
                if vcodec == 'none' and acodec ~= 'none' then
                    return 'format is unstable audio variant selector'
                end
                break
            end
        end
    end

    return nil
end

function M._clear_suspicious_ytdl_format_for_url(url, reason)
    url = trim(tostring(url or ''))
    if url == '' then
        return false
    end

    local raw = mp.get_property_native('ytdl-raw-info')
    local bad_props = {}
    local bad_value = nil
    local bad_reason = nil
    local checks = {
        {
            prop = 'ytdl-format',
            value = mp.get_property_native('ytdl-format'),
        },
        {
            prop = 'file-local-options/ytdl-format',
            value = mp.get_property('file-local-options/ytdl-format'),
        },
        {
            prop = 'options/ytdl-format',
            value = mp.get_property('options/ytdl-format'),
        },
    }

    for _, item in ipairs(checks) do
        local candidate = trim(tostring(item.value or ''))
        local why = M._suspicious_ytdl_format_reason(candidate, url, raw)
        if why then
            if not bad_value then
                bad_value = candidate
            end
            if not bad_reason then
                bad_reason = why
            end
            bad_props[#bad_props + 1] = tostring(item.prop)
        end
    end

    if #bad_props == 0 then
        return false
    end

    pcall(mp.set_property, 'options/ytdl-format', '')
    pcall(mp.set_property, 'file-local-options/ytdl-format', '')
    pcall(mp.set_property, 'ytdl-format', '')
    _lua_log(
        'ytdl-format: cleared suspicious selector=' .. tostring(bad_value or '')
            .. ' props=' .. table.concat(bad_props, ',')
            .. ' reason=' .. tostring(bad_reason or reason or 'invalid')
            .. ' url=' .. tostring(url)
    )
    return true
end

function M._prepare_ytdl_format_for_web_load(url, reason)
    url = trim(tostring(url or ''))
    if url == '' then
        return false
    end

    if M._clear_suspicious_ytdl_format_for_url(url, reason) then
        return true
    end

    local normalized_url = url:gsub('#.*$', '')
    local base, query = normalized_url:match('^([^?]+)%?(.*)$')
    if base then
        local kept = {}
        for pair in query:gmatch('[^&]+') do
            local raw_key = pair:match('^([^=]+)') or pair
            local key = tostring(_percent_decode(raw_key) or raw_key or ''):lower()
            local keep = true
            if key == 't' or key == 'start' or key == 'time_continue' or key == 'timestamp' or key == 'time' or key == 'begin' then
                keep = false
            elseif key:match('^utm_') then
                keep = false
            end
            if keep then
                kept[#kept + 1] = pair
            end
        end
        if #kept > 0 then
            normalized_url = base .. '?' .. table.concat(kept, '&')
        else
            normalized_url = base
        end
    end
    local explicit_reload_url = normalized_url
    explicit_reload_url = explicit_reload_url:gsub('^[%a][%w+%.%-]*://', '')
    explicit_reload_url = explicit_reload_url:gsub('^www%.', '')
    explicit_reload_url = explicit_reload_url:gsub('/+$', '')
    explicit_reload_url = explicit_reload_url:lower()

    local is_explicit_reload = (
        explicit_reload_url ~= ''
        and _skip_next_store_check_url ~= ''
        and explicit_reload_url == _skip_next_store_check_url
    )

    local active_props = {}
    local first_value = nil
    local checks = {
        {
            prop = 'ytdl-format',
            value = mp.get_property_native('ytdl-format'),
        },
        {
            prop = 'file-local-options/ytdl-format',
            value = mp.get_property('file-local-options/ytdl-format'),
        },
        {
            prop = 'options/ytdl-format',
            value = mp.get_property('options/ytdl-format'),
        },
    }

    for _, item in ipairs(checks) do
        local candidate = trim(tostring(item.value or ''))
        if candidate ~= '' then
            if not first_value then
                first_value = candidate
            end
            active_props[#active_props + 1] = tostring(item.prop) .. '=' .. candidate
        end
    end

    if #active_props == 0 then
        return false
    end

    if is_explicit_reload then
        _lua_log(
            'ytdl-format: preserving explicit reload selector reason=' .. tostring(reason or 'on-load')
                .. ' url=' .. tostring(url)
                .. ' values=' .. table.concat(active_props, '; ')
        )
        return false
    end

    if explicit_reload_url ~= '' and first_value and first_value ~= '' then
        M._ytdl_download_format_fallbacks[explicit_reload_url] = first_value
    end

    pcall(mp.set_property, 'options/ytdl-format', '')
    pcall(mp.set_property, 'file-local-options/ytdl-format', '')
    pcall(mp.set_property, 'ytdl-format', '')
    _lua_log(
        'ytdl-format: cleared stale selector=' .. tostring(first_value or '')
            .. ' reason=' .. tostring(reason or 'on-load')
            .. ' url=' .. tostring(url)
            .. ' values=' .. table.concat(active_props, '; ')
            .. ' fallback_cached=' .. tostring(first_value and first_value ~= '' and 'yes' or 'no')
    )
    return true
end

local function _normalize_url_for_store_lookup(url)
    url = trim(tostring(url or ''))
    if url == '' then
        return ''
    end

    url = url:gsub('#.*$', '')

    local base, query = url:match('^([^?]+)%?(.*)$')
    if base then
        local kept = {}
        for pair in query:gmatch('[^&]+') do
            local raw_key = pair:match('^([^=]+)') or pair
            local key = tostring(_percent_decode(raw_key) or raw_key or ''):lower()
            local keep = true
            if key == 't' or key == 'start' or key == 'time_continue' or key == 'timestamp' or key == 'time' or key == 'begin' then
                keep = false
            elseif key:match('^utm_') then
                keep = false
            end
            if keep then
                kept[#kept + 1] = pair
            end
        end
        if #kept > 0 then
            url = base .. '?' .. table.concat(kept, '&')
        else
            url = base
        end
    end

    url = url:gsub('^[%a][%w+%.%-]*://', '')
    url = url:gsub('^www%.', '')
    url = url:gsub('/+$', '')
    return url:lower()
end

function M._remember_ytdl_download_format(url, fmt)
    local normalized = _normalize_url_for_store_lookup(url)
    local value = trim(tostring(fmt or ''))
    if normalized == '' or value == '' then
        return false
    end
    M._ytdl_download_format_fallbacks[normalized] = value
    return true
end

function M._get_remembered_ytdl_download_format(url)
    local normalized = _normalize_url_for_store_lookup(url)
    if normalized == '' then
        return nil
    end
    local cached = trim(tostring((M._ytdl_download_format_fallbacks or {})[normalized] or ''))
    if cached == '' then
        return nil
    end
    return cached
end

local function _build_store_lookup_needles(url)
    local out = {}
    local seen = {}

    local function add(value)
        value = trim(tostring(value or ''))
        if value == '' then
            return
        end
        local key = value:lower()
        if seen[key] then
            return
        end
        seen[key] = true
        out[#out + 1] = value
    end

    local raw = trim(tostring(url or ''))
    add(raw)

    local without_fragment = raw:gsub('#.*$', '')
    add(without_fragment)

    local normalized = _normalize_url_for_store_lookup(raw)
    add(normalized)

    local schemeless = without_fragment:gsub('^[%a][%w+%.%-]*://', '')
    schemeless = schemeless:gsub('/+$', '')
    add(schemeless)
    add(schemeless:gsub('^www%.', ''))

    return out
end

local function _check_store_for_existing_url(store, url, cb)
    cb = cb or function() end
    url = trim(tostring(url or ''))
    if url == '' then
        cb(nil, 'missing url')
        return
    end

    local needles = _build_store_lookup_needles(url)
    local idx = 1

    local function run_next(last_err)
        if idx > #needles then
            cb(nil, last_err)
            return
        end

        local needle = tostring(needles[idx])
        idx = idx + 1
        local query = 'url:' .. needle

        _lua_log('store-check: probing global query=' .. tostring(query))
        _run_helper_request_async({ op = 'url-exists', data = { url = url, needles = { needle } }, quiet = true }, 5.0, function(resp, err)
            if resp and resp.success then
                local data = resp.data
                if type(data) ~= 'table' or #data == 0 then
                    run_next(nil)
                    return
                end
                cb(data, nil, needle)
                return
            end

            local details = trim(tostring(err or ''))
            if details == '' and type(resp) == 'table' then
                if resp.error and tostring(resp.error) ~= '' then
                    details = trim(tostring(resp.error))
                elseif resp.stderr and tostring(resp.stderr) ~= '' then
                    details = trim(tostring(resp.stderr))
                end
            end
            run_next(details ~= '' and details or nil)
        end)
    end

    run_next(nil)
end

local function _current_target()
    local path = mp.get_property('path')
    if not path or path == '' then
        return nil
    end
    local mem = _extract_target_from_memory_uri(path)
    if mem and mem ~= '' then
        return mem
    end
    return path
end

local ImageControl = {
    enabled = false,
    binding_names = {},
    pan_step = 0.05,
    pan_step_slow = 0.02,
    zoom_step = 0.45,
    zoom_step_slow = 0.15,
}

local MAX_IMAGE_ZOOM = 4.5

local function _install_q_block()
    pcall(mp.commandv, 'keybind', 'q', 'script-message', 'medeia-image-quit-block')
end

local function _restore_q_default()
    pcall(mp.commandv, 'keybind', 'q', 'quit')
end

local function _enable_image_section()
    pcall(mp.commandv, 'enable-section', 'image', 'allow-hide-cursor')
end

local function _disable_image_section()
    pcall(mp.commandv, 'disable-section', 'image')
end

mp.register_script_message('medeia-image-quit-block', function()
    if ImageControl.enabled then
        mp.osd_message('Press ESC if you really want to quit', 0.7)
        return
    end
    mp.commandv('quit')
end)

local ImageExtensions = {
    jpg = true,
    jpeg = true,
    png = true,
    gif = true,
    webp = true,
    bmp = true,
    tif = true,
    tiff = true,
    heic = true,
    heif = true,
    avif = true,
    ico = true,
}

local function _clean_path_for_extension(path)
    if type(path) ~= 'string' then
        return nil
    end
    local clean = path:match('([^?]+)') or path
    clean = clean:match('([^#]+)') or clean
    local last = clean:match('([^/\\]+)$') or ''
    local ext = last:match('%.([A-Za-z0-9]+)$')
    if not ext then
        return nil
    end
    return ext:lower()
end

local function _is_image_path(path)
    local ext = _clean_path_for_extension(path)
    return ext and ImageExtensions[ext]
end

local function _get_current_item_is_image()
    local video_info = mp.get_property_native('current-tracks/video')
    if type(video_info) == 'table' then
        if video_info.image == true then
            return true
        end
        if video_info.image == false then
            return false
        end
    end
    local target = _current_target()
    if target then
        return _is_image_path(target)
    end
    return false
end

function M._has_visual_screenshot_source()
    if _get_current_item_is_image() then
        return true, 'image'
    end

    local width = tonumber(mp.get_property_number('width') or 0) or 0
    local height = tonumber(mp.get_property_number('height') or 0) or 0
    if width > 0 and height > 0 then
        return true, 'video'
    end

    local video_params = mp.get_property_native('video-params')
    if type(video_params) == 'table' then
        local params_width = tonumber(video_params.w or video_params.dw or video_params.width or 0) or 0
        local params_height = tonumber(video_params.h or video_params.dh or video_params.height or 0) or 0
        if params_width > 0 and params_height > 0 then
            return true, 'video'
        end
    end

    return false, ''
end


local function _set_image_property(value)
    pcall(mp.set_property_native, 'user-data/mpv/image', value and true or false)
end

local function _show_image_status(message)
    local zoom = mp.get_property_number('video-zoom') or 0
    local pan_x = mp.get_property_number('video-pan-x') or 0
    local pan_y = mp.get_property_number('video-pan-y') or 0
    local zoom_percent = math.floor((1 + zoom) * 100 + 0.5)
    local text = string.format('Image: zoom %d%% pan %+.2f %+.2f', zoom_percent, pan_x, pan_y)
    if message and message ~= '' then
        text = message .. ' | ' .. text
    end
    mp.osd_message(text, 0.7)
end

local function _change_pan(dx, dy)
    local pan_x = mp.get_property_number('video-pan-x') or 0
    local pan_y = mp.get_property_number('video-pan-y') or 0
    mp.set_property_number('video-pan-x', pan_x + dx)
    mp.set_property_number('video-pan-y', pan_y + dy)
    _show_image_status()
end

local function _change_zoom(delta)
    local current = mp.get_property_number('video-zoom') or 0
    local target = current + delta
    if target > MAX_IMAGE_ZOOM then
        target = MAX_IMAGE_ZOOM
    end
    if target < -1.0 then
        target = -1.0
    end
    mp.set_property_number('video-zoom', target)
    mp.set_property('video-unscaled', 'no')
    if target >= MAX_IMAGE_ZOOM then
        mp.osd_message('Image zoom maxed at 450%', 0.7)
    else
        _show_image_status()
    end
end

local function _reset_pan_zoom()
    mp.set_property_number('video-pan-x', 0)
    mp.set_property_number('video-pan-y', 0)
    mp.set_property_number('video-zoom', 0)
    mp.set_property('video-align-x', '0')
    mp.set_property('video-align-y', '0')
    mp.set_property('panscan', 0)
    mp.set_property('video-unscaled', 'no')
    _show_image_status('Zoom reset')
end

local function _sanitize_filename_component(s)
    s = trim(tostring(s or ''))
    if s == '' then
        return 'screenshot'
    end
    -- Windows-unfriendly characters: <>:"/\|?* and control chars
    s = s:gsub('[%c]', '')
    s = s:gsub('[<>:"/\\|%?%*]', '_')
    s = trim(s)
    s = s:gsub('[%.%s]+$', '')
    if s == '' then
        return 'screenshot'
    end
    return s
end

local function _strip_title_extension(title, path)
    title = trim(tostring(title or ''))
    if title == '' then
        return title
    end
    path = tostring(path or '')
    local ext = path:match('%.([%w%d]+)$')
    if not ext or ext == '' then
        return title
    end
    ext = ext:lower()
    local suffix = '.' .. ext
    if title:lower():sub(-#suffix) == suffix then
        return trim(title:sub(1, #title - #suffix))
    end
    return title
end

local _pending_screenshot = nil

local function _normalize_tag_list(value)
    local tags = {}
    local seen = {}

    local function add_tag(text)
        text = trim(tostring(text or ''))
        if text == '' then
            return
        end
        local key = text:lower()
        if seen[key] then
            return
        end
        seen[key] = true
        tags[#tags + 1] = text
    end

    if type(value) == 'table' then
        for _, item in ipairs(value) do
            add_tag(item)
        end
        return tags
    end

    local text = tostring(value or '')
    for token in text:gmatch('[^,;\r\n]+') do
        add_tag(token)
    end
    return tags
end

local function _write_repl_queue_file_local(command_text, source_text, metadata)
    command_text = trim(tostring(command_text or ''))
    if command_text == '' then
        return nil, 'empty pipeline command'
    end

    local repo_root = _detect_repo_root()
    if repo_root == '' then
        return nil, 'repo root not found'
    end

    local log_dir = utils.join_path(repo_root, 'Log')
    if not _path_exists(log_dir) then
        return nil, 'Log directory not found'
    end

    local stamp = tostring(math.floor(mp.get_time() * 1000))
    local token = tostring(math.random(100000, 999999))
    local path = utils.join_path(log_dir, 'medeia-repl-queue-' .. stamp .. '-' .. token .. '.json')
    local payload = {
        id = stamp .. '-' .. token,
        command = command_text,
        source = trim(tostring(source_text or 'external')),
        created_at = os.time(),
    }
    if type(metadata) == 'table' and next(metadata) ~= nil then
        payload.metadata = metadata
    end

    local encoded = utils.format_json(payload)
    if type(encoded) ~= 'string' or encoded == '' then
        return nil, 'failed to encode queue payload'
    end

    local fh = io.open(path, 'w')
    if not fh then
        return nil, 'failed to open queue file'
    end
    fh:write(encoded)
    fh:close()
    return path, nil
end

local function _queue_pipeline_in_repl(pipeline_cmd, queued_message, failure_prefix, queue_label, metadata)
    pipeline_cmd = trim(tostring(pipeline_cmd or ''))
    if pipeline_cmd == '' then
        mp.osd_message((failure_prefix or 'REPL queue failed') .. ': empty pipeline command', 5)
        return false
    end

    do
        local repo_root = _detect_repo_root()
        local detail = 'REPL not running'
        if repo_root ~= '' then
            local log_dir = utils.join_path(repo_root, 'Log')
            if _path_exists(log_dir) then
                local state_path = utils.join_path(log_dir, 'medeia-repl-state.json')
                local fh = io.open(state_path, 'r')
                if fh then
                    local raw = fh:read('*a')
                    fh:close()
                    raw = trim(tostring(raw or ''))
                    if raw ~= '' then
                        local ok, payload = pcall(utils.parse_json, raw)
                        if ok and type(payload) == 'table' then
                            local status = trim(tostring(payload.status or 'running')):lower()
                            local updated_at = tonumber(payload.updated_at or 0)
                            local now = (os and os.time) and os.time() or nil
                            if status == '' or status == 'running' then
                                if updated_at and updated_at > 0 and now and (now - updated_at) <= 3 then
                                    detail = ''
                                end
                            end
                        end
                    end
                end
            end
        end
        if detail ~= '' then
            _lua_log(queue_label .. ': repl unavailable err=' .. detail)
            mp.osd_message((failure_prefix or 'REPL queue failed') .. ': ' .. detail, 5)
            return false
        end
    end

    local queue_metadata = { kind = 'mpv-download' }
    if type(metadata) == 'table' then
        for key, value in pairs(metadata) do
            queue_metadata[key] = value
        end
    end

    local ipc_path = trim(tostring(get_mpv_ipc_path() or ''))
    if ipc_path ~= '' then
        if type(queue_metadata.mpv_notify) == 'table' and trim(tostring(queue_metadata.mpv_notify.ipc_path or '')) == '' then
            queue_metadata.mpv_notify.ipc_path = ipc_path
        end
        if type(queue_metadata.mpv_callback) ~= 'table' then
            queue_metadata.mpv_callback = {
                ipc_path = ipc_path,
                script = mp.get_script_name(),
                message = 'medeia-pipeline-event',
            }
        end
    end

    _lua_log(queue_label .. ': queueing repl cmd=' .. pipeline_cmd)
    do
        local queue_path, queue_err = _write_repl_queue_file_local(
            pipeline_cmd,
            queue_label,
            queue_metadata
        )
        if queue_path then
            _lua_log(queue_label .. ': queued repl command locally path=' .. tostring(queue_path))
            mp.osd_message(tostring(queued_message or 'Queued in REPL'), 3)
            return true
        end
        _lua_log(queue_label .. ': local queue write failed err=' .. tostring(queue_err or 'unknown') .. '; falling back to helper')
    end

    ensure_mpv_ipc_server()
    if not ensure_pipeline_helper_running() then
        mp.osd_message((failure_prefix or 'REPL queue failed') .. ': helper not running', 5)
        return false
    end

    _run_helper_request_async(
        {
            op = 'queue-repl-command',
            data = {
                command = pipeline_cmd,
                source = queue_label,
                metadata = queue_metadata,
            },
        },
        4.0,
        function(resp, err)
            if resp and resp.success then
                local queue_path = trim(tostring(resp.path or ''))
                _lua_log(queue_label .. ': queued repl command path=' .. tostring(queue_path))
                mp.osd_message(tostring(queued_message or 'Queued in REPL'), 3)
                return
            end

            local err_text = tostring(err or '')
            if err_text:find('timeout waiting response', 1, true) ~= nil then
                _lua_log(queue_label .. ': queue ack timeout; assuming repl command queued')
                mp.osd_message(tostring(queued_message or 'Queued in REPL'), 3)
                return
            end

            local detail = tostring(err or (resp and resp.error) or 'unknown')
            _lua_log(queue_label .. ': queue failed err=' .. detail)
            mp.osd_message((failure_prefix or 'REPL queue failed') .. ': ' .. detail, 5)
        end
    )
    return true
end

mp.register_script_message('medeia-pipeline-event', function(json)
    local ok, payload = pcall(utils.parse_json, json)
    if not ok or type(payload) ~= 'table' then
        _lua_log('pipeline-event: invalid payload=' .. tostring(json or ''))
        return
    end

    local encoded = utils.format_json(payload)
    if type(encoded) == 'string' and encoded ~= '' then
        pcall(mp.set_property, 'user-data/medeia-last-pipeline-event', encoded)
    end

    local phase = _pipeline_progress_ui.trim(payload.phase)
    local event_name = _pipeline_progress_ui.trim(payload.event)
    local kind = _pipeline_progress_ui.trim(payload.kind)
    local title = _pipeline_progress_ui.kind_title(kind)

    local summary = ''
    local detail = ''
    if phase == 'started' then
        local command_text = _pipeline_progress_ui.trim(payload.command_text)
        summary = command_text ~= '' and ('Started: ' .. command_text) or ('Started: ' .. kind)
        detail = 'Queued job started'
    elseif phase == 'progress' then
        if event_name == 'pipe-percent' then
            local label = _pipeline_progress_ui.trim(payload.pipe_label ~= '' and payload.pipe_label or kind ~= '' and kind or 'pipeline')
            local percent = tonumber(payload.percent or 0) or 0
            summary = ('%s %d%%'):format(label, math.floor(percent + 0.5))
            detail = 'Processing'
        elseif event_name == 'status' then
            summary = _pipeline_progress_ui.trim(payload.text)
            detail = _pipeline_progress_ui.trim(payload.pipe_label ~= '' and payload.pipe_label or kind)
        elseif event_name == 'transfer' then
            local label = _pipeline_progress_ui.trim(payload.label ~= '' and payload.label or 'transfer')
            local percent = tonumber(payload.percent or 0)
            if percent then
                summary = ('%s %d%%'):format(label, math.floor(percent + 0.5))
            else
                summary = label
            end
            local completed = tonumber(payload.completed or 0)
            local total = tonumber(payload.total or 0)
            if completed and total and total > 0 then
                detail = ('%d / %d'):format(math.floor(completed + 0.5), math.floor(total + 0.5))
            end
        elseif event_name == 'pipe-begin' then
            local label = _pipeline_progress_ui.trim(payload.pipe_label ~= '' and payload.pipe_label or kind ~= '' and kind or 'pipeline')
            summary = 'Running: ' .. label
            local total_items = tonumber(payload.total_items or 0)
            if total_items and total_items > 0 then
                detail = ('Items: %d'):format(math.floor(total_items + 0.5))
            end
        elseif event_name == 'pipe-emit' then
            local label = _pipeline_progress_ui.trim(payload.pipe_label ~= '' and payload.pipe_label or kind ~= '' and kind or 'pipeline')
            local completed = tonumber(payload.completed or 0) or 0
            local total = tonumber(payload.total or 0) or 0
            summary = total > 0 and ('%s %d/%d'):format(label, completed, total) or label
            detail = _pipeline_progress_ui.trim(payload.item_label)
        end
    end

    if phase == 'completed' then
        pcall(mp.set_property, 'user-data/medeia-pipeline-progress', '')
        pcall(mp.set_property, 'user-data/medeia-pipeline-progress-summary', '')
        if payload.success == false then
            summary = title .. ' failed'
            detail = _pipeline_progress_ui.trim(payload.error)
            if detail == '' then
                detail = 'Unknown error'
            end
        else
            summary = title .. ' complete'
            detail = _pipeline_progress_ui.trim(payload.command_text)
        end
        _pipeline_progress_ui.update(title, summary, detail)
        _pipeline_progress_ui.schedule_hide(2.5)
    else
        if type(encoded) == 'string' and encoded ~= '' then
            pcall(mp.set_property, 'user-data/medeia-pipeline-progress', encoded)
        end
        if summary ~= '' then
            pcall(mp.set_property, 'user-data/medeia-pipeline-progress-summary', summary)
        end
        _pipeline_progress_ui.update(title, summary, detail)
    end

    _lua_log(
        'pipeline-event: phase=' .. tostring(payload.phase or '')
            .. ' event=' .. tostring(payload.event or '')
            .. ' success=' .. tostring(payload.success)
            .. ' kind=' .. tostring(payload.kind or '')
            .. ' error=' .. tostring(payload.error or '')
    )
end)

local function _start_screenshot_store_save(store, out_path, tags)
    store = _normalize_store_name(store)
    out_path = _normalize_fs_path(out_path)
    if store == '' or out_path == '' then
        mp.osd_message('Screenshot upload failed: invalid store or path', 5)
        return false
    end

    local is_named_store = _is_cached_store_name(store)
    local tag_list = _normalize_tag_list(tags)
    local screenshot_url = trim(tostring((_current_url_for_web_actions and _current_url_for_web_actions()) or mp.get_property(CURRENT_WEB_URL_PROP) or ''))
    if screenshot_url == '' or not screenshot_url:match('^https?://') then
        screenshot_url = ''
    end
    local cmd = 'file -add -plugin hydrusnetwork -instance ' .. quote_pipeline_arg(store)
        .. ' ' .. quote_pipeline_arg(out_path)
    if screenshot_url ~= '' then
        cmd = cmd .. ' -url ' .. quote_pipeline_arg(screenshot_url)
    end
    if is_named_store then
        _set_selected_store(store)
    end

    local tag_suffix = (#tag_list > 0) and (' | tags: ' .. tostring(#tag_list)) or ''
    if #tag_list > 0 then
        local tag_string = table.concat(tag_list, ',')
        cmd = cmd .. ' | tag -add ' .. quote_pipeline_arg(tag_string)
    end

    local queue_target = is_named_store and ('store ' .. store) or 'folder'
    local success_text = is_named_store and ('Screenshot saved to store: ' .. store .. tag_suffix) or ('Screenshot saved to folder' .. tag_suffix)
    local failure_text = is_named_store and 'Screenshot upload failed' or 'Screenshot save failed'

    _lua_log('screenshot-save: queueing repl pipeline cmd=' .. cmd)

    return _queue_pipeline_in_repl(
        cmd,
        'Queued in REPL: screenshot -> ' .. queue_target .. tag_suffix,
        'Screenshot queue failed',
        'screenshot-save',
        {
            kind = 'mpv-screenshot',
            mpv_notify = {
                success_text = success_text,
                failure_text = failure_text,
                duration_ms = 3500,
            },
        }
    )
end

local function _commit_pending_screenshot(tags)
    if type(_pending_screenshot) ~= 'table' or not _pending_screenshot.path or not _pending_screenshot.store then
        return
    end
    local store = tostring(_pending_screenshot.store or '')
    local out_path = tostring(_pending_screenshot.path or '')
    _pending_screenshot = nil
    _start_screenshot_store_save(store, out_path, tags)
end

local function _apply_screenshot_tag_query(query)
    M._close_uosc_menu_and_sync(SCREENSHOT_TAG_MENU_TYPE, 'screenshot-tags-submit')
    _commit_pending_screenshot(_normalize_tag_list(query))
end

local function _open_screenshot_tag_prompt(store, out_path)
    store = _normalize_store_name(store)
    out_path = _normalize_fs_path(out_path)
    if store == '' or out_path == '' then
        return
    end

    _pending_screenshot = { store = store, path = out_path }

    if not ensure_uosc_loaded() then
        _commit_pending_screenshot(nil)
        return
    end

    local menu_data = {
        type = SCREENSHOT_TAG_MENU_TYPE,
        title = 'Screenshot tags',
        search_style = 'palette',
        search_debounce = 'submit',
        on_search = { 'script-message-to', mp.get_script_name(), 'medeia-image-screenshot-tags-search' },
        footnote = 'Optional comma-separated tags. Press Enter to save, or choose Save without tags.',
        items = {
            {
                title = 'Save without tags',
                hint = 'Skip optional tags',
                value = { 'script-message-to', mp.get_script_name(), 'medeia-image-screenshot-tags-event', utils.format_json({ query = '' }) },
            },
        },
    }

    if not M._open_uosc_menu(menu_data, 'screenshot-tag-prompt') then
        _commit_pending_screenshot(nil)
    end
end

local function _open_store_picker_for_pending_screenshot()
    if type(_pending_screenshot) ~= 'table' or not _pending_screenshot.path then
        return
    end

    local function build_items()
        local selected = _get_selected_store()
        local visible_names = M._visible_store_names()
        local items = {}

        items[#items + 1] = {
            title = 'Pick folder…',
            hint = 'Save screenshot to a local folder',
            value = { 'script-message-to', mp.get_script_name(), 'medeia-image-screenshot-pick-path', '{}' },
        }

        if #visible_names > 0 then
            for _, name in ipairs(visible_names) do
                name = trim(tostring(name or ''))
                if name ~= '' then
                    items[#items + 1] = {
                        title = name,
                        hint = (selected ~= '' and name == selected) and 'Current store' or '',
                        active = (selected ~= '' and name == selected) and true or false,
                        value = { 'script-message-to', mp.get_script_name(), 'medeia-image-screenshot-pick-store', utils.format_json({ store = name }) },
                    }
                end
            end
        elseif #items == 0 then
            items[#items + 1] = {
                title = 'No stores found',
                hint = 'Configure stores in config.conf',
                selectable = false,
            }
        end

        return items
    end

    _uosc_open_list_picker(DOWNLOAD_STORE_MENU_TYPE, 'Save screenshot', build_items())

    mp.add_timeout(0.05, function()
        if type(_pending_screenshot) ~= 'table' or not _pending_screenshot.path then
            return
        end
        _refresh_store_cache(1.5, function(success, changed)
            if success and changed then
                _uosc_open_list_picker(DOWNLOAD_STORE_MENU_TYPE, 'Save screenshot', build_items())
            end
        end)
    end)
end

local function _capture_screenshot()
    local screenshot_available = false
    local screenshot_kind = ''
    screenshot_available, screenshot_kind = M._has_visual_screenshot_source()
    if not screenshot_available then
        _lua_log('screenshot: blocked reason=no visual source')
        M._sync_uosc_cursor('screenshot-unavailable')
        mp.osd_message('Screenshot unavailable (no video/image frame)', 2)
        return
    end

    local function _format_time_label(seconds)
        local total = math.max(0, math.floor(tonumber(seconds or 0) or 0))
        local hours = math.floor(total / 3600)
        local minutes = math.floor(total / 60) % 60
        local secs = total % 60
        local parts = {}
        if hours > 0 then
            table.insert(parts, ('%dh'):format(hours))
        end
        if minutes > 0 or hours > 0 then
            table.insert(parts, ('%dm'):format(minutes))
        end
        table.insert(parts, ('%ds'):format(secs))
        return table.concat(parts)
    end

    local time = mp.get_property_number('time-pos') or mp.get_property_number('time') or 0
    local label = _format_time_label(time)

    local raw_title = trim(tostring(mp.get_property('media-title') or ''))
    local raw_path = tostring(mp.get_property('path') or '')
    if raw_title == '' then
        raw_title = 'screenshot'
    end
    raw_title = _strip_title_extension(raw_title, raw_path)
    local safe_title = _sanitize_filename_component(raw_title)

    local filename = safe_title .. '_' .. label .. '.png'
    local temp_dir = _normalize_fs_path(mp.get_property('user-data/medeia-config-temp'))
    if temp_dir == '' then
        temp_dir = _normalize_fs_path(os.getenv('TEMP') or os.getenv('TMP') or '/tmp')
    end
    local out_path = utils.join_path(temp_dir, filename)
    out_path = _normalize_fs_path(out_path)
    local was_paused = mp.get_property_native('pause') and true or false

    if not was_paused then
        pcall(mp.set_property_native, 'pause', true)
    end

    local function do_screenshot(mode)
        mode = mode or 'video'
        local ok, err = pcall(function()
            return mp.commandv('screenshot-to-file', out_path, mode)
        end)
        return ok, err
    end

    -- Try 'video' first (no OSD). If that fails (e.g. audio mode without video/art),
    -- try 'window' as fallback.
    local ok = do_screenshot('video')
    if not ok then
        _lua_log('screenshot: video-mode failed; trying window-mode kind=' .. tostring(screenshot_kind))
        ok = do_screenshot('window')
    end

    if not ok then
        _lua_log('screenshot: BOTH video and window modes FAILED')
        if not was_paused then
            pcall(mp.set_property_native, 'pause', false)
        end
        M._sync_uosc_cursor('screenshot-failed')
        mp.osd_message('Screenshot failed (no frames)', 2)
        return
    end

    if not was_paused then
        mp.osd_message('Screenshot captured. Playback paused for review.', 2)
    end

    _ensure_selected_store_loaded()

    local function dispatch_screenshot_save()
        local visible_store_names = M._visible_store_names()
        local store_count = #visible_store_names
        local selected_store = _normalize_store_name(_get_selected_store())
        if not M._store_name_is_visible_in_mpv(selected_store) then
            selected_store = ''
        end

        if store_count > 1 then
            _pending_screenshot = { path = out_path }
            _open_store_picker_for_pending_screenshot()
            return
        end

        if selected_store == '' and store_count == 1 then
            selected_store = _normalize_store_name(visible_store_names[1])
        end

        if selected_store == '' then
            _pending_screenshot = { path = out_path }
            _open_store_picker_for_pending_screenshot()
            return
        end

        _open_screenshot_tag_prompt(selected_store, out_path)
    end

    if not _store_cache_loaded then
        _refresh_store_cache(1.5, function()
            dispatch_screenshot_save()
        end)
    else
        dispatch_screenshot_save()
    end
end

mp.register_script_message('medeia-image-screenshot', function()
    _capture_screenshot()
end)

mp.register_script_message('medeia-image-screenshot-pick-store', function(json)
    if type(_pending_screenshot) ~= 'table' or not _pending_screenshot.path then
        return
    end
    local ok, ev = pcall(utils.parse_json, json)
    if not ok or type(ev) ~= 'table' then
        return
    end

    local store = _normalize_store_name(ev.store)
    if store == '' then
        return
    end

    local out_path = tostring(_pending_screenshot.path or '')
    _open_screenshot_tag_prompt(store, out_path)
end)

mp.register_script_message('medeia-image-screenshot-pick-path', function()
    if type(_pending_screenshot) ~= 'table' or not _pending_screenshot.path then
        return
    end

    M._pick_folder_async(function(folder, err)
        if err and err ~= '' then
            mp.osd_message('Folder picker failed: ' .. tostring(err), 4)
            return
        end
        if not folder or folder == '' then
            return
        end

        local out_path = tostring(_pending_screenshot.path or '')
        _open_screenshot_tag_prompt(folder, out_path)
    end)
end)

mp.register_script_message('medeia-image-screenshot-tags-search', function(query)
    _apply_screenshot_tag_query(query)
end)

mp.register_script_message('medeia-image-screenshot-tags-event', function(json)
    local ok, ev = pcall(utils.parse_json, json)
    if not ok or type(ev) ~= 'table' then
        return
    end
    _apply_screenshot_tag_query(ev.query)
end)


local CLIP_MARKER_SLOT_COUNT = 2
local clip_markers = {}
local initial_chapters = nil

local function _format_clip_marker_label(time)
    if type(time) ~= 'number' then
        return '0s'
    end
    local total = math.max(0, math.floor(time))
    local hours = math.floor(total / 3600)
    local minutes = math.floor(total / 60) % 60
    local seconds = total % 60
    local parts = {}
    if hours > 0 then
        table.insert(parts, ('%dh'):format(hours))
    end
    if minutes > 0 or hours > 0 then
        table.insert(parts, ('%dm'):format(minutes))
    end
    table.insert(parts, ('%ds'):format(seconds))
    return table.concat(parts)
end

local function _apply_clip_chapters()
    local chapters = {}
    if initial_chapters then
        for _, chapter in ipairs(initial_chapters) do table.insert(chapters, chapter) end
    end
    for idx = 1, CLIP_MARKER_SLOT_COUNT do
        local time = clip_markers[idx]
        if time and type(time) == 'number' then
            table.insert(chapters, {
                time = time,
                title = _format_clip_marker_label(time),
            })
        end
    end
    table.sort(chapters, function(a, b) return (a.time or 0) < (b.time or 0) end)
    mp.set_property_native('chapter-list', chapters)
end

local function _reset_clip_markers()
    for idx = 1, CLIP_MARKER_SLOT_COUNT do
        clip_markers[idx] = nil
    end
    _apply_clip_chapters()
end

local function _capture_clip()
    local time = mp.get_property_number('time-pos') or mp.get_property_number('time')
    if not time then
        mp.osd_message('Cannot capture clip; no time available', 0.7)
        return
    end
    local slot = nil
    for idx = 1, CLIP_MARKER_SLOT_COUNT do
        if not clip_markers[idx] then
            slot = idx
            break
        end
    end
    if not slot then
        local best = math.huge
        for idx = 1, CLIP_MARKER_SLOT_COUNT do
            local existing = clip_markers[idx]
            local distance = math.abs((existing or 0) - time)
            if distance < best then
                best = distance
                slot = idx
            end
        end
        slot = slot or 1
    end
    clip_markers[slot] = time
    _apply_clip_chapters()
    mp.commandv('screenshot-to-file', ('clip-%s-%.0f.png'):format(os.date('%Y%m%d-%H%M%S'), time))
    local label = _format_clip_marker_label(time)
    mp.osd_message(('Clip marker %d set at %s'):format(slot, label), 0.7)
end

mp.register_event('file-loaded', function()
    initial_chapters = mp.get_property_native('chapter-list') or {}
    _reset_clip_markers()
end)

mp.register_script_message('medeia-image-clip', function()
    _capture_clip()
end)

local function _get_trim_range_from_clip_markers()
    local times = {}
    for idx = 1, CLIP_MARKER_SLOT_COUNT do
        local t = clip_markers[idx]
        if type(t) == 'number' then
            table.insert(times, t)
        end
    end
    table.sort(times, function(a, b) return a < b end)
    if #times < 2 then
        return nil
    end
    local start_t = times[1]
    local end_t = times[2]
    if type(start_t) ~= 'number' or type(end_t) ~= 'number' then
        return nil
    end
    if end_t <= start_t then
        return nil
    end
    return _format_clip_marker_label(start_t) .. '-' .. _format_clip_marker_label(end_t)
end

local function _audio_only()
    mp.commandv('set', 'vid', 'no')
    mp.osd_message('Audio-only playback enabled', 1)
end

mp.register_script_message('medeia-audio-only', function()
    _audio_only()
end)

local function _cancel_sleep_timer(show_message)
    if _sleep_timer ~= nil then
        pcall(function()
            _sleep_timer:kill()
        end)
        _sleep_timer = nil
    end
    if show_message then
        mp.osd_message('Sleep timer cancelled', 1.5)
    end
end

local function _parse_sleep_minutes(text)
    local s = trim(tostring(text or '')):lower()
    if s == '' then
        return nil
    end

    if s == 'off' or s == 'cancel' or s == 'stop' or s == '0' then
        return 0
    end

    local hours = s:match('^([%d%.]+)%s*h$')
    if hours then
        local value = tonumber(hours)
        if value and value > 0 then
            return value * 60
        end
        return nil
    end

    local mins = s:match('^([%d%.]+)%s*m$')
    if mins then
        local value = tonumber(mins)
        if value and value >= 0 then
            return value
        end
        return nil
    end

    local value = tonumber(s)
    if value and value >= 0 then
        return value
    end

    return nil
end

local function _open_sleep_timer_prompt()
    local items = {
        {
            title = '15 minutes',
            hint = 'Quick preset',
            value = { 'script-message-to', mp.get_script_name(), 'medeia-sleep-timer-event', utils.format_json({ type = 'search', query = '15' }) },
        },
        {
            title = '30 minutes',
            hint = 'Quick preset',
            value = { 'script-message-to', mp.get_script_name(), 'medeia-sleep-timer-event', utils.format_json({ type = 'search', query = '30' }) },
        },
        {
            title = '60 minutes',
            hint = 'Quick preset',
            value = { 'script-message-to', mp.get_script_name(), 'medeia-sleep-timer-event', utils.format_json({ type = 'search', query = '60' }) },
        },
        {
            title = 'Cancel timer',
            hint = 'Also accepts off / 0 / cancel',
            value = { 'script-message-to', mp.get_script_name(), 'medeia-sleep-timer-event', utils.format_json({ type = 'search', query = '0' }) },
        },
    }

    local menu_data = {
        type = SLEEP_PROMPT_MENU_TYPE,
        title = 'Sleep Timer',
        search_style = 'palette',
        search_debounce = 'submit',
        on_search = { 'script-message-to', mp.get_script_name(), 'medeia-sleep-timer-search' },
        footnote = 'Enter minutes (30), or use 1h / 1.5h. Enter 0 to cancel.',
        items = items,
    }

    if M._open_uosc_menu(menu_data, 'sleep-timer-prompt') then
        return
    else
        mp.osd_message('Sleep timer unavailable (uosc not loaded)', 2.0)
    end
end

local function _apply_sleep_timer_query(query)
    local minutes = _parse_sleep_minutes(query)
    if minutes == nil then
        mp.osd_message('Sleep timer: enter minutes, 1h, or 0 to cancel', 2.0)
        return
    end

    if minutes <= 0 then
        _cancel_sleep_timer(true)
        M._close_uosc_menu_and_sync(SLEEP_PROMPT_MENU_TYPE, 'sleep-timer-cancel')
        return
    end

    _cancel_sleep_timer(false)

    local seconds = math.max(1, math.floor(minutes * 60))
    _sleep_timer = mp.add_timeout(seconds, function()
        _sleep_timer = nil
        mp.osd_message('Sleep timer: closing mpv', 1.5)
        mp.commandv('quit')
    end)

    mp.osd_message(string.format('Sleep timer set: %d min', math.floor(minutes + 0.5)), 1.5)
    _lua_log('sleep: timer set minutes=' .. tostring(minutes) .. ' seconds=' .. tostring(seconds))

    M._close_uosc_menu_and_sync(SLEEP_PROMPT_MENU_TYPE, 'sleep-timer-submit')
end

local function _handle_sleep_timer_event(json)
    local ok, ev = pcall(utils.parse_json, json)
    if not ok or type(ev) ~= 'table' then
        _lua_log('sleep: invalid event payload=' .. tostring(json))
        return
    end

    if ev.type ~= 'search' then
        return
    end

    _apply_sleep_timer_query(ev.query)
end

mp.register_script_message('medeia-sleep-timer', function()
    _open_sleep_timer_prompt()
end)

mp.register_script_message('medeia-sleep-timer-event', function(json)
    _handle_sleep_timer_event(json)
end)

mp.register_script_message('medeia-sleep-timer-search', function(query)
    _apply_sleep_timer_query(query)
end)

local function _bind_image_key(key, name, fn, opts)
    opts = opts or {}
    if ImageControl.binding_names[name] then
        pcall(mp.remove_key_binding, name)
        ImageControl.binding_names[name] = nil
    end
    local ok, err = pcall(mp.add_forced_key_binding, key, name, fn, opts)
    if ok then
        ImageControl.binding_names[name] = true
    else
        mp.msg.warn('Failed to add image binding ' .. tostring(key) .. ': ' .. tostring(err))
    end
end

local function _unbind_image_keys()
    for name in pairs(ImageControl.binding_names) do
        pcall(mp.remove_key_binding, name)
        ImageControl.binding_names[name] = nil
    end
end

local function _activate_image_controls()
    if ImageControl.enabled then
        return
    end
    ImageControl.enabled = true
    _set_image_property(true)
    _enable_image_section()
    mp.osd_message('Image viewer controls enabled', 1.2)

    _bind_image_key('LEFT', 'image-pan-left', function() _change_pan(-ImageControl.pan_step, 0) end, {repeatable=true})
    _bind_image_key('RIGHT', 'image-pan-right', function() _change_pan(ImageControl.pan_step, 0) end, {repeatable=true})
    _bind_image_key('s', 'image-pan-s', function() _change_pan(0, ImageControl.pan_step) end, {repeatable=true})
    _bind_image_key('a', 'image-pan-a', function() _change_pan(ImageControl.pan_step, 0) end, {repeatable=true})
    _bind_image_key('d', 'image-pan-d', function() _change_pan(-ImageControl.pan_step, 0) end, {repeatable=true})
    _bind_image_key('Shift+RIGHT', 'image-pan-right-fine', function() _change_pan(ImageControl.pan_step_slow, 0) end, {repeatable=true})
    _bind_image_key('Shift+UP', 'image-pan-up-fine', function() _change_pan(0, -ImageControl.pan_step_slow) end, {repeatable=true})
    _bind_image_key('Shift+DOWN', 'image-pan-down-fine', function() _change_pan(0, ImageControl.pan_step_slow) end, {repeatable=true})
    _bind_image_key('h', 'image-pan-h', function() _change_pan(-ImageControl.pan_step, 0) end, {repeatable=true})
    _bind_image_key('l', 'image-pan-l', function() _change_pan(ImageControl.pan_step, 0) end, {repeatable=true})
    _bind_image_key('j', 'image-pan-j', function() _change_pan(0, ImageControl.pan_step) end, {repeatable=true})
    _bind_image_key('k', 'image-pan-k', function() _change_pan(0, -ImageControl.pan_step) end, {repeatable=true})
    _bind_image_key('w', 'image-pan-w', function() _change_pan(0, -ImageControl.pan_step) end, {repeatable=true})
    _bind_image_key('s', 'image-pan-s', function() _change_pan(0, ImageControl.pan_step) end, {repeatable=true})
    _bind_image_key('a', 'image-pan-a', function() _change_pan(ImageControl.pan_step, 0) end, {repeatable=true})
    _bind_image_key('d', 'image-pan-d', function() _change_pan(-ImageControl.pan_step, 0) end, {repeatable=true})

    _bind_image_key('=', 'image-zoom-in', function() _change_zoom(ImageControl.zoom_step) end, {repeatable=true})
    _bind_image_key('-', 'image-zoom-out', function() _change_zoom(-ImageControl.zoom_step) end, {repeatable=true})
    _bind_image_key('+', 'image-zoom-in-fine', function() _change_zoom(ImageControl.zoom_step_slow) end, {repeatable=true})
    _bind_image_key('_', 'image-zoom-out-fine', function() _change_zoom(-ImageControl.zoom_step_slow) end, {repeatable=true})
    _bind_image_key('0', 'image-zoom-reset', _reset_pan_zoom)
    _bind_image_key('Space', 'image-status', function() _show_image_status('Image status') end)
    _bind_image_key('f', 'image-screenshot', _capture_screenshot)
    _install_q_block()
end

local function _deactivate_image_controls()
    if not ImageControl.enabled then
        _disable_image_section()
        return
    end
    ImageControl.enabled = false
    _set_image_property(false)
    _disable_image_section()
    _restore_q_default()
    _unbind_image_keys()
    mp.osd_message('Image viewer controls disabled', 1.0)
    mp.set_property('panscan', 0)
    mp.set_property('video-zoom', 0)
    mp.set_property_number('video-pan-x', 0)
    mp.set_property_number('video-pan-y', 0)
    mp.set_property('video-align-x', '0')
    mp.set_property('video-align-y', '0')
end

local function _update_image_mode()
    local should_image = _get_current_item_is_image()
    if should_image then
        _activate_image_controls()
    else
        _deactivate_image_controls()
    end
end

mp.register_event('file-loaded', function()
    _update_image_mode()
    if not _get_current_item_is_image() then
        M._schedule_uosc_cursor_resync('file-loaded')
    end
end)

mp.register_event('shutdown', function()
    _restore_q_default()
end)

_update_image_mode()

local function _extract_store_hash(target)
    if type(target) ~= 'string' or target == '' then
        return nil
    end
    local hash = _extract_query_param(target, 'hash')
    local store = _extract_query_param(target, 'store')
    if hash and store then
        local h = tostring(hash):lower()
        if h:match('^[0-9a-f]+$') and #h == 64 then
            return { store = tostring(store), hash = h }
        end
    end
    return nil
end

function M._pick_folder_python_async(cb)
    cb = cb or function() end

    local python = _resolve_python_exe(false)
    if not python or python == '' then
        cb(nil, 'no python executable available')
        return
    end

    local bootstrap = table.concat({
        'import sys',
        'try:',
        '    import tkinter as tk',
        '    from tkinter import filedialog',
        '    root = tk.Tk()',
        '    root.withdraw()',
        '    try:',
        '        root.wm_attributes("-topmost", 1)',
        '    except Exception:',
        '        pass',
        '    root.update()',
        '    path = filedialog.askdirectory(title="Select download folder", mustexist=False)',
        '    root.destroy()',
        '    if path:',
        '        sys.stdout.write(path)',
        'except Exception as exc:',
        '    sys.stderr.write(f"{type(exc).__name__}: {exc}")',
        '    raise SystemExit(2)',
    }, '\n')

    _lua_log('folder-picker: spawning async dialog backend=python-tk')
    mp.command_native_async(
        {
            name = 'subprocess',
            args = { python, '-c', bootstrap },
            capture_stdout = true,
            capture_stderr = true,
            playback_only = false,
        },
        function(success, result, err)
            if not success then
                cb(nil, tostring(err or 'subprocess failed'))
                return
            end
            if type(result) ~= 'table' then
                cb(nil, 'invalid subprocess result')
                return
            end

            local status = tonumber(result.status or 0) or 0
            local stdout = trim(tostring(result.stdout or ''))
            local stderr = trim(tostring(result.stderr or ''))
            if status ~= 0 then
                local detail = stderr ~= '' and stderr or tostring(result.error or ('folder picker exited with status ' .. tostring(status)))
                cb(nil, detail)
                return
            end
            if stdout == '' then
                cb(nil, nil)
                return
            end
            cb(stdout, nil)
        end
    )
end

function M._pick_folder_async(cb)
    cb = cb or function() end
    local jit_mod = rawget(_G, 'jit')
    local platform_name = tostring((type(jit_mod) == 'table' and jit_mod.os) or ''):lower()
    local function handle_result(label, success, result, err, fallback_cb)
        if not success then
            local detail = tostring(err or 'subprocess failed')
            _lua_log('folder-picker: subprocess failed backend=' .. tostring(label) .. ' err=' .. tostring(detail))
            if fallback_cb then
                fallback_cb(detail)
                return
            end
            cb(nil, detail)
            return
        end
        if type(result) ~= 'table' then
            _lua_log('folder-picker: invalid subprocess result backend=' .. tostring(label))
            if fallback_cb then
                fallback_cb('invalid subprocess result')
                return
            end
            cb(nil, 'invalid subprocess result')
            return
        end

        local status = tonumber(result.status or 0) or 0
        local stdout = trim(tostring(result.stdout or ''))
        local stderr = trim(tostring(result.stderr or ''))
        if status ~= 0 then
            local detail = stderr
            if detail == '' then
                detail = tostring(result.error or ('folder picker exited with status ' .. tostring(status)))
            end
            _lua_log('folder-picker: failed backend=' .. tostring(label) .. ' detail=' .. tostring(detail))
            if fallback_cb then
                fallback_cb(detail)
                return
            end
            cb(nil, detail)
            return
        end

        if stdout == '' then
            _lua_log('folder-picker: cancelled backend=' .. tostring(label))
            cb(nil, nil)
            return
        end

        _lua_log('folder-picker: selected path=' .. tostring(stdout) .. ' backend=' .. tostring(label))
        cb(stdout, nil)
    end

    local function spawn_picker(label, args, fallback_cb)
        _lua_log('folder-picker: spawning async dialog backend=' .. tostring(label))
        mp.command_native_async(
            {
                name = 'subprocess',
                args = args,
                capture_stdout = true,
                capture_stderr = true,
                playback_only = false,
            },
            function(success, result, err)
                handle_result(label, success, result, err, fallback_cb)
            end
        )
    end

    if _is_windows() then
        local ps = [[
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$owner = New-Object System.Windows.Forms.Form
$owner.Text = 'medeia-folder-owner'
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
$owner.Location = New-Object System.Drawing.Point(-32000, -32000)
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.ShowInTaskbar = $false
$owner.TopMost = $true
$owner.Opacity = 0
$owner.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedToolWindow
$owner.Add_Shown({ $owner.Activate() })
$null = $owner.Show()
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = 'Select download folder'
$d.ShowNewFolderButton = $true
try {
    if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        $d.SelectedPath
    }
} finally {
    $d.Dispose()
    $owner.Close()
    $owner.Dispose()
}
]]
        spawn_picker('windows-winforms', { 'powershell', '-NoProfile', '-WindowStyle', 'Hidden', '-STA', '-ExecutionPolicy', 'Bypass', '-Command', ps }, function(detail)
            _lua_log('folder-picker: windows native picker failed; trying python-tk detail=' .. tostring(detail or ''))
            M._pick_folder_python_async(cb)
        end)
        return
    end

    if platform_name == 'osx' or platform_name == 'macos' then
        spawn_picker('macos-osascript', { 'osascript', '-e', 'POSIX path of (choose folder with prompt "Select download folder")' }, function(detail)
            _lua_log('folder-picker: macos native picker failed; trying python-tk detail=' .. tostring(detail or ''))
            M._pick_folder_python_async(cb)
        end)
        return
    end

    spawn_picker('linux-native', {
        'sh',
        '-lc',
        'if command -v zenity >/dev/null 2>&1; then zenity --file-selection --directory --title="Select download folder"; ' ..
            'elif command -v kdialog >/dev/null 2>&1; then kdialog --getexistingdirectory "$HOME" --title "Select download folder"; ' ..
            'elif command -v yad >/dev/null 2>&1; then yad --file-selection --directory --title="Select download folder"; ' ..
            'else exit 127; fi'
    }, function(detail)
        _lua_log('folder-picker: linux native picker failed; trying python-tk detail=' .. tostring(detail or ''))
        M._pick_folder_python_async(cb)
    end)
end

M._pick_folder_windows_async = M._pick_folder_async

local function _store_names_key(names)
    if type(names) ~= 'table' or #names == 0 then
        return ''
    end
    local normalized = {}
    for _, name in ipairs(names) do
        normalized[#normalized + 1] = trim(tostring(name or ''))
    end
    return table.concat(normalized, '\0')
end

local function _run_pipeline_request_async(pipeline_cmd, seeds, timeout_seconds, cb)
    cb = cb or function() end
    pipeline_cmd = trim(tostring(pipeline_cmd or ''))
    if pipeline_cmd == '' then
        cb(nil, 'empty pipeline command')
        return
    end
    ensure_mpv_ipc_server()
    local req = { pipeline = pipeline_cmd }
    if seeds then
        req.seeds = seeds
    end
    _run_helper_request_async(req, timeout_seconds or 30, cb)
end

_refresh_store_cache = function(timeout_seconds, on_complete)
    ensure_mpv_ipc_server()

    local prev_count = (type(_cached_store_names) == 'table') and #_cached_store_names or 0
    local prev_key = _store_names_key(_cached_store_names)
    local had_previous = prev_count > 0

    local function apply_store_choices(resp, source)
        if not resp or type(resp) ~= 'table' or type(resp.choices) ~= 'table' then
            _lua_log('stores: ' .. tostring(source) .. ' result missing choices table; resp_type=' .. tostring(type(resp)))
            return false
        end

        local out = {}
        for _, v in ipairs(resp.choices) do
            local name = trim(tostring(v or ''))
            if name ~= '' then
                out[#out + 1] = name
            end
        end
        if #out == 0 and had_previous then
            _lua_log('stores: ignoring empty ' .. tostring(source) .. ' payload; keeping previous store list')
            if type(on_complete) == 'function' then
                on_complete(true, false)
            end
            return true
        end

        _cached_store_names = out
        _store_cache_loaded = (#out > 0) or _store_cache_loaded
        pcall(M._save_store_names_to_disk, out)

        local payload = utils.format_json({ choices = out })
        if type(payload) == 'string' and payload ~= '' then
            pcall(mp.set_property, 'user-data/medeia-store-choices-cached', payload)
        end

        local preview = ''
        if #out > 0 then
            preview = table.concat(out, ', ')
        end
        _lua_log('stores: loaded ' .. tostring(#out) .. ' stores from ' .. tostring(source) .. ': ' .. tostring(preview))
        if type(on_complete) == 'function' then
            on_complete(true, _store_names_key(out) ~= prev_key)
        end
        return true
    end

    local cached_json = mp.get_property('user-data/medeia-store-choices-cached')
    if cached_json and cached_json ~= '' then
        local ok, cached_resp = pcall(utils.parse_json, cached_json)
        if ok then
            if type(cached_resp) == 'string' then
                ok, cached_resp = pcall(utils.parse_json, cached_resp)
            end
            if ok then
                if apply_store_choices(cached_resp, 'cache') then
                    return true
                end
            end
        else
            _lua_log('stores: cached property parse failed; falling back to direct config')
        end
    end

    if not _store_cache_retry_pending then
        _store_cache_retry_pending = true
        M._load_store_choices_direct_async(function(resp, err)
            _store_cache_retry_pending = false
            if apply_store_choices(resp, 'direct config') then
                return
            end

            _lua_log('stores: direct config load failed error=' .. tostring(err or 'unknown'))
            if had_previous then
                _lua_log('stores: keeping previous store list after direct config failure count=' .. tostring(prev_count))
                if type(on_complete) == 'function' then
                    on_complete(true, false)
                end
            elseif type(on_complete) == 'function' then
                on_complete(false, false)
            end
        end)
    else
        _lua_log('stores: direct config refresh already pending')
    end
    return false
end

_uosc_open_list_picker = function(menu_type, title, items)
    local menu_data = {
        type = menu_type,
        title = title,
        items = items or {},
    }
    if not M._open_uosc_menu(menu_data, 'list-picker:' .. tostring(menu_type or title or 'menu')) then
        _lua_log('menu: uosc not available; cannot open-menu')
    end
end

local function _open_store_picker()
    _ensure_selected_store_loaded()

    local selected = _get_selected_store()
    local visible_store_names = M._visible_store_names()
    local cached_count = #visible_store_names
    local cached_preview = ''
    if cached_count > 0 then
        cached_preview = table.concat(visible_store_names, ', ')
    end
    _lua_log(
        'stores: open picker selected='
            .. tostring(selected)
            .. ' cached_count='
            .. tostring(cached_count)
            .. ' cached='
            .. tostring(cached_preview)
    )

    local function build_items()
        local selected = _get_selected_store()
        local current_url = trim(tostring(_current_url_for_web_actions() or _current_target() or ''))
        local visible_names = M._visible_store_names()
        local items = {}

        if #visible_names > 0 then
            for _, name in ipairs(visible_names) do
                name = trim(tostring(name or ''))
                if name ~= '' then
                    local payload = { store = name }
                    local hint = _store_status_hint_for_url(name, current_url, nil)
                    if (not hint or hint == '') and selected ~= '' and name == selected then
                        hint = 'Current store'
                    end
                    items[#items + 1] = {
                        title = name,
                        hint = hint,
                        active = (selected ~= '' and name == selected) and true or false,
                        value = { 'script-message-to', mp.get_script_name(), 'medeia-store-select', utils.format_json(payload) },
                    }
                end
            end
        else
            items[#items + 1] = {
                title = 'No stores found',
                hint = 'Configure stores in config.conf',
                selectable = false,
            }
        end

        return items
    end

    -- Open immediately with whatever cache we have.
    _uosc_open_list_picker(STORE_PICKER_MENU_TYPE, 'Store', build_items())

    mp.add_timeout(0.05, function()
        _refresh_store_cache(1.2, function(success, changed)
            if success and changed then
                _lua_log('stores: reopening menu (store list changed)')
                _uosc_open_list_picker(STORE_PICKER_MENU_TYPE, 'Store', build_items())
            end
        end)
    end)
end

mp.register_script_message('medeia-store-picker', function()
    _open_store_picker()
end)

mp.register_script_message('medeia-store-select', function(json)
    local ok, ev = pcall(utils.parse_json, json)
    if not ok or type(ev) ~= 'table' then
        return
    end
    local store = trim(tostring(ev.store or ''))
    if store == '' then
        return
    end
    _set_selected_store(store)
    mp.osd_message('Store: ' .. store, 2)
    _refresh_current_store_url_status('store-select')
end)

-- No-op handler for placeholder menu items.
mp.register_script_message('medios-nop', function()
    return
end)

local _pending_download = nil
local _pending_format_change = nil

-- Per-file state (class-like) for format caching.
M.FileState = {}
M.FileState.__index = M.FileState

function M.FileState.new()
    return setmetatable({
        url = nil,
        formats = nil,
    }, M.FileState)
end

function M.FileState:set_url(url)
    local normalized = trim(tostring(url or ''))
    if normalized == '' then
        self.url = nil
        self.formats = nil
        self.formats_table = nil
        return
    end
    if self.url ~= normalized then
        self.url = normalized
        self.formats = nil
        self.formats_table = nil
    end
end

function M.FileState:has_formats()
    return type(self.formats) == 'table'
        and type(self.formats.rows) == 'table'
        and #self.formats.rows > 0
end

function M.FileState:set_formats(url, tbl)
    self:set_url(url)
    self.formats = tbl
    self.formats_table = tbl
end

M.file = M.file or M.FileState.new()

-- Cache yt-dlp format lists per URL so Change Format is instant.
local _formats_cache = {}
local _formats_inflight = {}
local _formats_waiters = {}
local _formats_prefetch_retries = {}
local _format_cache_poll_generation = 0
local _last_raw_format_summary = ''

local function _is_http_url(u)
    if type(u) ~= 'string' then
        return false
    end
    return u:match('^https?://') ~= nil
end

local _ytdlp_domains_cached = nil

local function _is_ytdlp_url(u)
    if not u or type(u) ~= 'string' then
        return false
    end
    local low = trim(u:lower())
    if not low:match('^https?://') then
        return false
    end

    -- Fast exclusions for things we know are not meant for yt-dlp format switching
    if low:find('/get_files/file', 1, true) then return false end
    if low:find('tidal.com/manifest', 1, true) then return false end
    if low:find('alldebrid.com/f/', 1, true) then return false end

    -- Try to use the cached domain list from the pipeline helper
    local domains_str = mp.get_property('user-data/medeia-ytdlp-domains-cached') or ''
    if domains_str ~= '' then
        if not _ytdlp_domains_cached then
            _ytdlp_domains_cached = {}
            for d in domains_str:gmatch('%S+') do
                _ytdlp_domains_cached[d] = true
            end
        end

        local host = low:match('^https?://([^/]+)')
        if host then
            -- Remove port if present
            host = host:match('^([^:]+)')
            
            -- Check direct match or parent domain matches
            local parts = {}
            for p in host:gmatch('[^%.]+') do
                table.insert(parts, p)
            end

            -- Check from full domain down to top-level (e.g. m.youtube.com, youtube.com)
            for i = 1, #parts - 1 do
                local candidate = table.concat(parts, '.', i)
                if _ytdlp_domains_cached[candidate] then
                    return true
                end
            end
        end
    end

    -- Fallback/Hardcoded: Probable video/audio sites for which Change Format is actually useful
    local patterns = {
        'youtube%.com', 'youtu%.be', 'vimeo%.com', 'twitch%.tv',
        'soundcloud%.com', 'bandcamp%.com', 'bilibili%.com',
        'dailymotion%.com', 'pixiv%.net', 'twitter%.com', 
        'x%.com', 'instagram%.com', 'tiktok%.com', 'reddit%.com',
        'facebook%.com', 'fb%.watch'
    }
    for _, p in ipairs(patterns) do
        if low:match(p) then
            return true
        end
    end

    -- If we have formats already cached for this URL, it's definitely supported
    if M._get_cached_formats_table(u) then
        return true
    end

    return false
end

local function _set_current_web_url(url)
    local normalized = trim(tostring(url or ''))
    if normalized ~= '' and _is_http_url(normalized) then
        pcall(mp.set_property, CURRENT_WEB_URL_PROP, normalized)
        if type(M.file) == 'table' and M.file.set_url then
            M.file:set_url(normalized)
        end
        return normalized
    end
    pcall(mp.set_property, CURRENT_WEB_URL_PROP, '')
    if type(M.file) == 'table' and M.file.set_url then
        M.file:set_url(nil)
    end
    return nil
end

local function _get_current_web_url()
    local current = trim(tostring(mp.get_property(CURRENT_WEB_URL_PROP) or ''))
    if current ~= '' and _is_http_url(current) then
        return current
    end
    return nil
end

_current_url_for_web_actions = function()
    local current = _get_current_web_url()
    if current and current ~= '' then
        return current
    end
    local target = _current_target()
    if not target or target == '' then
        return nil
    end
    return tostring(target)
end

function M._build_web_ytdl_raw_options()
    local raw = trim(tostring(mp.get_property('ytdl-raw-options') or ''))
    if raw == '' then
        raw = trim(tostring(mp.get_property('options/ytdl-raw-options') or ''))
    end

    local lower = raw:lower()
    local extra = {}

    if not lower:find('write%-subs=', 1) then
        extra[#extra + 1] = 'write-subs='
    end
    if not lower:find('write%-auto%-subs=', 1) then
        extra[#extra + 1] = 'write-auto-subs='
    end
    if not lower:find('sub%-langs=', 1) then
        extra[#extra + 1] = 'sub-langs=[en.*,en,-live_chat]'
    end
    if not lower:find('sub%-format=', 1) then
        -- Prefer chunked subtitle formats over word-by-word JSON tracks.
        extra[#extra + 1] = 'sub-format=srt/vtt/best'
    end

    if #extra == 0 then
        return raw ~= '' and raw or nil
    end
    if raw ~= '' then
        return raw .. ',' .. table.concat(extra, ',')
    end
    return table.concat(extra, ',')
end

function M._prime_web_subtitle_global_defaults(reason)
    local raw = M._build_web_ytdl_raw_options()
    if not raw or raw == '' then
        return false
    end
    pcall(mp.set_property, 'options/ytdl-raw-options', raw)
    pcall(mp.set_property, 'ytdl-raw-options', raw)
    _lua_log('web-subtitles: primed global ytdl defaults reason=' .. tostring(reason or 'startup'))
    return true
end

function M._resolve_web_subtitle_target(preferred_target)
    local explicit = trim(tostring(preferred_target or ''))
    if explicit ~= '' and _is_http_url(explicit) and _is_ytdlp_url(explicit) then
        return explicit
    end

    local path_target = trim(tostring(mp.get_property('path') or mp.get_property('stream-open-filename') or ''))
    if path_target ~= '' and _is_http_url(path_target) and _is_ytdlp_url(path_target) then
        return path_target
    end

    local current = _get_current_web_url()
    if current and current ~= '' and _is_http_url(current) and _is_ytdlp_url(current) then
        return current
    end

    return nil
end

function M._apply_web_subtitle_load_defaults(reason, preferred_target)
    local target = M._resolve_web_subtitle_target(preferred_target)
    if not target then
        return false
    end

    _set_current_web_url(target)
    M._prepare_ytdl_format_for_web_load(target, reason or 'on-load')

    local raw = M._build_web_ytdl_raw_options()
    if raw and raw ~= '' then
        pcall(mp.set_property, 'file-local-options/ytdl-raw-options', raw)
    end
    pcall(mp.set_property, 'file-local-options/sub-visibility', 'yes')
    pcall(mp.set_property, 'file-local-options/sid', 'auto')
    pcall(mp.set_property, 'file-local-options/track-auto-selection', 'yes')
    -- Strip ASS positioning/styling for web subtitles to keep lines anchored naturally.
    pcall(mp.set_property, 'file-local-options/sub-ass-override', 'strip')
    _lua_log('web-subtitles: prepared load defaults reason=' .. tostring(reason or 'on-load') .. ' target=' .. tostring(target))
    return true
end

function M._find_subtitle_track_candidate()
    local tracks = mp.get_property_native('track-list')
    if type(tracks) ~= 'table' then
        return nil, nil, false
    end

    local function subtitle_track_blob(track)
        local parts = {}
        local fields = { 'lang', 'title', 'name', 'external-filename' }
        for _, key in ipairs(fields) do
            local value = ''
            if type(track) == 'table' then
                value = trim(tostring(track[key] or '')):lower()
            end
            if value ~= '' then
                parts[#parts + 1] = value
            end
        end
        return table.concat(parts, ' ')
    end

    local function subtitle_track_is_english(track, blob)
        local lang = ''
        if type(track) == 'table' then
            lang = trim(tostring(track.lang or '')):lower()
        end
        if lang == 'en' or lang == 'eng' or lang:match('^en[-_]') or lang:match('^eng[-_]') then
            return true
        end
        local text = blob or subtitle_track_blob(track)
        if text:match('%f[%a]english%f[%A]') then
            return true
        end
        return false
    end

    local function subtitle_track_is_autogenerated(track, blob)
        local text = blob or subtitle_track_blob(track)
        local markers = {
            'auto-generated',
            'auto generated',
            'autogenerated',
            'automatic captions',
            'automatic subtitles',
            'generated automatically',
            'asr',
        }
        for _, marker in ipairs(markers) do
            if text:find(marker, 1, true) then
                return true
            end
        end
        return false
    end

    local best_id = nil
    local best_source = nil
    local best_selected = false
    local best_score = nil

    for _, track in ipairs(tracks) do
        if type(track) == 'table' and tostring(track.type or '') == 'sub' and not track.albumart then
            local id = tonumber(track.id)
            if id then
                local blob = subtitle_track_blob(track)
                local selected = track.selected and true or false
                local source = 'fallback'
                local score = 100

                if blob:find('medeia-sub', 1, true) then
                    source = 'medeia-note'
                    score = 1000
                else
                    local english = subtitle_track_is_english(track, blob)
                    local autogenerated = subtitle_track_is_autogenerated(track, blob)
                    if english and not autogenerated then
                        source = 'english-manual'
                        score = 800
                    elseif english and autogenerated then
                        source = 'english-auto'
                        score = 700
                    elseif selected then
                        source = 'selected'
                        score = 300
                    elseif track.default then
                        source = 'default'
                        score = 200
                    else
                        source = 'first'
                        score = 100
                    end
                end

                if selected then
                    score = score + 50
                end
                if track.default then
                    score = score + 25
                end
                if type(track.external) == 'boolean' and track.external then
                    score = score + 10
                end

                if best_score == nil or score > best_score then
                    best_score = score
                    best_id = id
                    best_source = source
                    best_selected = selected
                end
            end
        end
    end

    if best_id ~= nil then
        return best_id, best_source, best_selected
    end
    return nil, nil, false
end

function M._ensure_current_subtitles_visible(reason)
    local state = M._subtitle_autoselect_state or { serial = 0, deadline = 0 }
    M._subtitle_autoselect_state = state
    if (mp.get_time() or 0) > (state.deadline or 0) then
        return false
    end

    local current = _get_current_web_url()
    local path = trim(tostring(mp.get_property('path') or ''))
    if (not current or current == '') and (path == '' or not _is_http_url(path)) then
        return false
    end
    if (not current or current == '') and (path == '' or not _is_ytdlp_url(path)) then
        return false
    end

    local track_id, source, already_selected = M._find_subtitle_track_candidate()
    if not track_id then
        return false
    end

    pcall(mp.set_property, 'sub-visibility', 'yes')
    if already_selected then
        return true
    end

    local ok = pcall(mp.set_property_native, 'sid', track_id)
    if ok then
        _lua_log('web-subtitles: selected subtitle track id=' .. tostring(track_id) .. ' source=' .. tostring(source or 'unknown') .. ' reason=' .. tostring(reason or 'auto'))
    end
    return ok and true or false
end

function M._schedule_web_subtitle_activation(reason)
    local state = M._subtitle_autoselect_state or { serial = 0, deadline = 0 }
    M._subtitle_autoselect_state = state
    state.serial = (state.serial or 0) + 1
    local serial = state.serial
    state.deadline = (mp.get_time() or 0) + 12.0
    local delays = { 0.15, 0.75, 2.0, 5.0 }
    for _, delay in ipairs(delays) do
        mp.add_timeout(delay, function()
            local current_state = M._subtitle_autoselect_state or {}
            if serial ~= current_state.serial then
                return
            end
            M._ensure_current_subtitles_visible((reason or 'file-loaded') .. '@' .. tostring(delay))
        end)
    end
end

function M._sync_current_web_url_from_playback()
    local target = _current_target()
    local target_str = trim(tostring(target or ''))
    if target_str ~= '' and _extract_store_hash(target_str) then
        _set_current_web_url(nil)
        return
    end
    if target_str ~= '' and _is_http_url(target_str) and _is_ytdlp_url(target_str) then
        _set_current_web_url(target_str)
        return
    end

    local current = _get_current_web_url()
    if current and current ~= '' then
        local raw = mp.get_property_native('ytdl-raw-info')
        if type(raw) == 'table' then
            if type(M.file) == 'table' and M.file.set_url then
                M.file:set_url(current)
            end
            return
        end
    end

    if target_str == '' or not _is_http_url(target_str) then
        _set_current_web_url(nil)
    end
end

mp.add_hook('on_load', 50, function()
    local ok, err = pcall(M._apply_web_subtitle_load_defaults, 'on_load')
    if not ok then
        _lua_log('web-subtitles: on_load setup failed err=' .. tostring(err))
    end
end)

local _current_store_url_status = {
    generation = 0,
    store = '',
    url = '',
    status = 'idle',
    err = '',
    match_count = 0,
    needle = '',
}

local function _set_current_store_url_status(store, url, status, err, match_count, needle)
    _current_store_url_status.store = trim(tostring(store or ''))
    _current_store_url_status.url = trim(tostring(url or ''))
    _current_store_url_status.status = trim(tostring(status or 'idle'))
    _current_store_url_status.err = trim(tostring(err or ''))
    _current_store_url_status.match_count = tonumber(match_count or 0) or 0
    _current_store_url_status.needle = trim(tostring(needle or ''))
end

local function _begin_current_store_url_status(store, url, status, err, match_count, needle)
    _current_store_url_status.generation = (_current_store_url_status.generation or 0) + 1
    _set_current_store_url_status(store, url, status, err, match_count, needle)
    return _current_store_url_status.generation
end

local function _current_store_url_status_matches(store, url)
    local current_url = _normalize_url_for_store_lookup(_current_store_url_status.url)
    local target_url = _normalize_url_for_store_lookup(url)
    return current_url ~= '' and current_url == target_url
end

_store_status_hint_for_url = function(store, url, fallback)
    store = trim(tostring(store or ''))
    url = trim(tostring(url or ''))
    if not M._store_name_is_visible_in_mpv(store) then
        return fallback
    end
    if store == '' or url == '' or not _current_store_url_status_matches(store, url) then
        return fallback
    end

    local status = tostring(_current_store_url_status.status or '')
    if status == 'pending' or status == 'checking' then
        return 'Checking current URL'
    end
    if status == 'found' then
        return 'Current URL already exists'
    end
    if status == 'missing' then
        return fallback or 'Current URL not found'
    end
    if status == 'error' then
        if fallback and fallback ~= '' then
            return fallback .. ' | lookup unavailable'
        end
        return 'Lookup unavailable'
    end
    return fallback
end

_refresh_current_store_url_status = function(reason)
    reason = trim(tostring(reason or ''))
    _ensure_selected_store_loaded()

    local store = trim(tostring(_get_selected_store() or ''))
    local target = _current_url_for_web_actions() or _current_target()
    local url = trim(tostring(target or ''))
    local normalized_url = _normalize_url_for_store_lookup(url)

    if not M._store_name_is_visible_in_mpv(store) then
        _begin_current_store_url_status(store, url, 'idle')
        return
    end

    if url == '' or not _is_http_url(url) or _extract_store_hash(url) then
        _begin_current_store_url_status(store, url, 'idle')
        return
    end

    if normalized_url ~= '' and _skip_next_store_check_url ~= '' and normalized_url == _skip_next_store_check_url then
        _lua_log('store-check: skipped reason=' .. tostring(reason) .. ' url=' .. tostring(url) .. ' (format reload)')
        _skip_next_store_check_url = ''
        return
    end

    local generation = _begin_current_store_url_status(store, url, 'pending')
    if not _is_pipeline_helper_ready() then
        _lua_log('store-check: helper not ready; skipping lookup reason=' .. tostring(reason) .. ' store=' .. tostring(store))
        _set_current_store_url_status(store, url, 'idle')
        return
    end

    _set_current_store_url_status(store, url, 'checking')
    _lua_log('store-check: starting reason=' .. tostring(reason) .. ' store=' .. tostring(store) .. ' url=' .. tostring(url))

    _check_store_for_existing_url(store, url, function(matches, err, needle)
        if _current_store_url_status.generation ~= generation then
            return
        end

        local active_store = trim(tostring(_get_selected_store() or ''))
        local active_target = _current_url_for_web_actions() or _current_target()
        local active_url = trim(tostring(active_target or ''))
        if active_store ~= store or active_url ~= url then
            _lua_log('store-check: stale response discarded store=' .. tostring(store) .. ' url=' .. tostring(url))
            return
        end

        if type(matches) == 'table' and #matches > 0 then
            _set_current_store_url_status(store, url, 'found', nil, #matches, needle)
            _lua_log('store-check: found matches=' .. tostring(#matches) .. ' needle=' .. tostring(needle or ''))
            return
        end

        local err_text = trim(tostring(err or ''))
        if err_text ~= '' then
            _set_current_store_url_status(store, url, 'error', err_text, 0, needle)
            _lua_log('store-check: lookup unavailable')
            return
        end

        _set_current_store_url_status(store, url, 'missing', nil, 0, needle)
        _lua_log('store-check: missing url=' .. tostring(url))
    end)
end

function M._cache_formats_for_url(url, tbl)
    if type(url) ~= 'string' or url == '' then
        return
    end
    if type(tbl) ~= 'table' then
        return
    end
    _formats_cache[url] = { table = tbl, ts = mp.get_time() }
    if type(M.file) == 'table' and M.file.set_formats then
        M.file:set_formats(url, tbl)
    else
        M.file.url = url
        M.file.formats = tbl
        M.file.formats_table = tbl
    end
end

function M._get_cached_formats_table(url)
    if type(url) ~= 'string' or url == '' then
        return nil
    end
    local hit = _formats_cache[url]
    if type(hit) == 'table' and type(hit.table) == 'table' then
        return hit.table
    end
    return nil
end

function M._format_bytes_compact(size_bytes)
    local value = tonumber(size_bytes)
    if not value or value <= 0 then
        return ''
    end
    local units = { 'B', 'KB', 'MB', 'GB', 'TB' }
    local unit_index = 1
    while value >= 1024 and unit_index < #units do
        value = value / 1024
        unit_index = unit_index + 1
    end
    if unit_index == 1 then
        return tostring(math.floor(value + 0.5)) .. units[unit_index]
    end
    return string.format('%.1f%s', value, units[unit_index])
end

function M._is_browseable_raw_format(fmt)
    if type(fmt) ~= 'table' then
        return false
    end

    local format_id = trim(tostring(fmt.format_id or ''))
    if format_id == '' then
        return false
    end

    local ext = trim(tostring(fmt.ext or '')):lower()
    if ext == 'mhtml' or ext == 'json' then
        return false
    end

    local note = trim(tostring(fmt.format_note or fmt.format or '')):lower()
    if note:find('storyboard', 1, true) then
        return false
    end

    if format_id:lower():match('^sb') then
        return false
    end

    local protocol = trim(tostring(fmt.protocol or '')):lower()
    local size_bytes = fmt.filesize or fmt.filesize_approx
    if protocol ~= ''
        and (protocol == 'm3u8' or protocol == 'm3u8_native')
        and format_id:match('^%d+%-%d+$')
        and not size_bytes then
        local hls_vcodec = tostring(fmt.vcodec or 'none')
        local hls_acodec = tostring(fmt.acodec or 'none')
        if hls_vcodec ~= 'none' and hls_acodec ~= 'none' then
            return false
        end
    end

    local vcodec = tostring(fmt.vcodec or 'none')
    local acodec = tostring(fmt.acodec or 'none')
    if vcodec == 'none' and acodec == 'none' then
        return false
    end

    -- Audio-only rows are redundant (video rows already get "+ba" auto-muxed)
    -- and their collapsed DRC-variant selector ids are frequently not
    -- resolvable by yt-dlp on their own, which breaks Change Format selection.
    if vcodec == 'none' and acodec ~= 'none' then
        return false
    end

    return true
end

function M._raw_format_display_id(fmt)
    local format_id = trim(tostring(fmt and fmt.format_id or ''))
    if format_id == '' then
        return ''
    end
    local vcodec = tostring(fmt and fmt.vcodec or 'none')
    local acodec = tostring(fmt and fmt.acodec or 'none')
    if vcodec == 'none' and acodec ~= 'none' then
        local base = format_id:match('^(%d+)%-%w+$')
        if base and base ~= '' then
            return base
        end
    end
    return format_id
end

function M._raw_format_selection_id(fmt)
    local display_id = M._raw_format_display_id(fmt)
    if display_id == '' then
        return ''
    end
    local vcodec = tostring(fmt and fmt.vcodec or 'none')
    local acodec = tostring(fmt and fmt.acodec or 'none')
    if vcodec ~= 'none' and acodec == 'none' then
        return display_id .. '+ba'
    end
    return display_id
end

function M._raw_info_has_audio_variant_selectors(raw)
    if type(raw) ~= 'table' or type(raw.formats) ~= 'table' then
        return false
    end

    for _, fmt in ipairs(raw.formats) do
        if type(fmt) == 'table' then
            local format_id = trim(tostring(fmt.format_id or ''))
            local vcodec = tostring(fmt.vcodec or 'none')
            local acodec = tostring(fmt.acodec or 'none')
            if format_id:match('^%d+%-%w+$') and vcodec == 'none' and acodec ~= 'none' then
                return true
            end
        end
    end

    return false
end

function M._raw_format_picker_score(fmt)
    local note = trim(tostring(fmt and (fmt.format_note or fmt.format) or '')):lower()
    local format_id = trim(tostring(fmt and fmt.format_id or '')):lower()
    local prefers_original = (note:find('original', 1, true) or note:find('default', 1, true)) and 1 or 0
    local avoids_drc = (format_id:find('-drc', 1, true) or note:find('drc', 1, true)) and 0 or 1
    local magnitude = tonumber(fmt and (fmt.filesize or fmt.filesize_approx or fmt.abr or fmt.tbr) or 0) or 0
    return prefers_original * 1000000000000 + avoids_drc * 1000000000 + magnitude
end

function M._build_formats_table_from_raw_info(url, raw)
    if raw == nil then
        raw = mp.get_property_native('ytdl-raw-info')
    end
    if type(raw) ~= 'table' then
        return nil, 'missing ytdl-raw-info'
    end

    local formats = raw.formats
    if type(formats) ~= 'table' or #formats == 0 then
        return nil, 'ytdl-raw-info has no formats'
    end

    local rows = {}
    local browseable_count = 0
    local seen_selection_ids = {}
    for _, fmt in ipairs(formats) do
        if M._is_browseable_raw_format(fmt) then
            browseable_count = browseable_count + 1
            local format_id = trim(tostring(fmt.format_id or ''))
            local display_id = M._raw_format_display_id(fmt)
            local resolution = trim(tostring(fmt.resolution or ''))
            if resolution == '' then
                local width = tonumber(fmt.width)
                local height = tonumber(fmt.height)
                if width and height then
                    resolution = tostring(math.floor(width)) .. 'x' .. tostring(math.floor(height))
                elseif height then
                    resolution = tostring(math.floor(height)) .. 'p'
                end
            end

            local ext = trim(tostring(fmt.ext or ''))
            local size = M._format_bytes_compact(fmt.filesize or fmt.filesize_approx)
            local selection_id = M._raw_format_selection_id(fmt)
            if selection_id ~= '' then
                local candidate = {
                    columns = {
                        { name = 'ID', value = display_id ~= '' and display_id or format_id },
                        { name = 'Resolution', value = resolution },
                        { name = 'Ext', value = ext },
                        { name = 'Size', value = size },
                    },
                    selection_args = { '-format', selection_id },
                    _picker_score = M._raw_format_picker_score(fmt),
                }
                local existing_index = seen_selection_ids[selection_id]
                if existing_index then
                    local existing = rows[existing_index]
                    local existing_score = tonumber(existing and existing._picker_score or 0) or 0
                    if candidate._picker_score > existing_score then
                        rows[existing_index] = candidate
                    end
                else
                    rows[#rows + 1] = candidate
                    seen_selection_ids[selection_id] = #rows
                end
            end
        end
    end

    for _, row in ipairs(rows) do
        row._picker_score = nil
    end

    if browseable_count == 0 then
        return { title = 'Formats', rows = {} }, nil
    end

    return { title = 'Formats', rows = rows }, nil
end

function M._summarize_formats_table(tbl, limit)
    if type(tbl) ~= 'table' or type(tbl.rows) ~= 'table' then
        return 'rows=0'
    end
    limit = tonumber(limit or 6) or 6
    local parts = {}
    for i = 1, math.min(#tbl.rows, limit) do
        local row = tbl.rows[i] or {}
        local cols = row.columns or {}
        local id_val = ''
        local res_val = ''
        for _, c in ipairs(cols) do
            if c.name == 'ID' then id_val = tostring(c.value or '') end
            if c.name == 'Resolution' then res_val = tostring(c.value or '') end
        end
        parts[#parts + 1] = id_val ~= '' and (id_val .. (res_val ~= '' and ('@' .. res_val) or '')) or ('row' .. tostring(i))
    end
    return 'rows=' .. tostring(#tbl.rows) .. ' sample=' .. table.concat(parts, ', ')
end

function M._cache_formats_from_raw_info(url, raw, source_label)
    url = trim(tostring(url or ''))
    if url == '' then
        return nil, 'missing url'
    end

    if raw == nil then
        raw = mp.get_property_native('ytdl-raw-info')
    end

    if M._raw_info_has_audio_variant_selectors(raw) then
        return nil, 'raw info requires validated probe'
    end

    local tbl, err = M._build_formats_table_from_raw_info(url, raw)
    if type(tbl) ~= 'table' or type(tbl.rows) ~= 'table' then
        return nil, err or 'raw format conversion failed'
    end

    M._cache_formats_for_url(url, tbl)
    local summary = M._summarize_formats_table(tbl, 8)
    local signature = url .. '|' .. summary
    if signature ~= _last_raw_format_summary then
        _last_raw_format_summary = signature
        _lua_log('formats: cached from ytdl-raw-info source=' .. tostring(source_label or 'unknown') .. ' ' .. summary)
    end
    return tbl, nil
end

function M._build_format_picker_items(tbl)
    local items = {}
    if type(tbl) ~= 'table' or type(tbl.rows) ~= 'table' then
        return items
    end
    for idx, row in ipairs(tbl.rows) do
        local cols = row.columns or {}
        local id_val = ''
        local res_val = ''
        local ext_val = ''
        local size_val = ''
        for _, c in ipairs(cols) do
            if c.name == 'ID' then id_val = tostring(c.value or '') end
            if c.name == 'Resolution' then res_val = tostring(c.value or '') end
            if c.name == 'Ext' then ext_val = tostring(c.value or '') end
            if c.name == 'Size' then size_val = tostring(c.value or '') end
        end
        local label = id_val ~= '' and id_val or ('Format ' .. tostring(idx))
        local hint_parts = {}
        if res_val ~= '' and res_val ~= 'N/A' then table.insert(hint_parts, res_val) end
        if ext_val ~= '' then table.insert(hint_parts, ext_val) end
        if size_val ~= '' and size_val ~= 'N/A' then table.insert(hint_parts, size_val) end
        local hint = table.concat(hint_parts, ' | ')

        local payload = { index = idx }
        items[#items + 1] = {
            title = label,
            hint = hint,
            value = { 'script-message-to', mp.get_script_name(), 'medios-change-format-pick', utils.format_json(payload) },
        }
    end
    return items
end

function M._open_format_picker_for_table(url, tbl)
    if type(tbl) ~= 'table' or type(tbl.rows) ~= 'table' or #tbl.rows == 0 then
        mp.osd_message('No formats available', 4)
        return false
    end

    _pending_format_change = _pending_format_change or { url = url, token = 'cached' }
    _pending_format_change.url = url
    _pending_format_change.formats_table = tbl

    local items = M._build_format_picker_items(tbl)
    M._debug_dump_formatted_formats(url, tbl, items)
    M._show_format_list_osd(items, 8)
    _uosc_open_list_picker(DOWNLOAD_FORMAT_MENU_TYPE, 'Change format', items)
    return true
end

function M._cache_formats_from_current_playback(reason, raw)
    local target = _current_url_for_web_actions() or _current_target()
    if not target or target == '' then
        return false, 'no current target'
    end
    local url = tostring(target)
    if not _is_http_url(url) then
        return false, 'target not http'
    end

    local tbl, err = M._cache_formats_from_raw_info(url, raw, reason)
    if type(tbl) == 'table' and type(tbl.rows) == 'table' then
        _lua_log('formats: playback cache ready source=' .. tostring(reason or 'unknown') .. ' rows=' .. tostring(#tbl.rows))
        if type(_pending_format_change) == 'table'
            and tostring(_pending_format_change.url or '') == url
            and type(_pending_format_change.formats_table) ~= 'table' then
            _lua_log('change-format: fulfilling pending picker from playback cache')
            M._open_format_picker_for_table(url, tbl)
        end
        return true, nil
    end
    return false, err
end

function M._run_formats_probe_async(url, cb)
    cb = cb or function() end
    url = trim(tostring(url or ''))
    if url == '' then
        cb(nil, 'missing url')
        return
    end

    local python = _resolve_python_exe(false)
    if not python or python == '' then
        cb(nil, 'no python executable available')
        return
    end

    local probe_script = _detect_format_probe_script()
    if probe_script == '' then
        cb(nil, 'format_probe.py not found')
        return
    end

    local cwd = _detect_repo_root()
    _lua_log('formats-probe: spawning subprocess python=' .. tostring(python) .. ' script=' .. tostring(probe_script) .. ' cwd=' .. tostring(cwd or '') .. ' url=' .. tostring(url))

    mp.command_native_async(
        {
            name = 'subprocess',
            args = { python, probe_script, url },
            capture_stdout = true,
            capture_stderr = true,
            playback_only = false,
        },
        function(success, result, err)
            if not success then
                cb(nil, tostring(err or 'subprocess failed'))
                return
            end

            if type(result) ~= 'table' then
                cb(nil, 'invalid subprocess result')
                return
            end

            local status = tonumber(result.status or 0) or 0
            local stdout = trim(tostring(result.stdout or ''))
            local stderr = trim(tostring(result.stderr or ''))
            if stdout == '' then
                local detail = stderr
                if detail == '' then
                    detail = tostring(result.error or ('format probe exited with status ' .. tostring(status)))
                end
                cb(nil, detail)
                return
            end

            local ok, payload = pcall(utils.parse_json, stdout)
            if (not ok or type(payload) ~= 'table') and stdout ~= '' then
                local last_json_line = nil
                for line in stdout:gmatch('[^\r\n]+') do
                    line = trim(tostring(line or ''))
                    if line ~= '' then
                        last_json_line = line
                    end
                end
                if last_json_line and last_json_line ~= stdout then
                    _lua_log('formats-probe: retrying json parse from last stdout line')
                    ok, payload = pcall(utils.parse_json, last_json_line)
                end
            end
            if not ok or type(payload) ~= 'table' then
                cb(nil, 'invalid format probe json')
                return
            end

            if not payload.success then
                local detail = tostring(payload.error or payload.stderr or stderr or 'format probe failed')
                cb(payload, detail)
                return
            end

            cb(payload, nil)
        end
    )
end

function M._schedule_playback_format_cache_poll(url, generation, attempt)
    if type(url) ~= 'string' or url == '' or generation ~= _format_cache_poll_generation then
        return
    end
    if M._get_cached_formats_table(url) then
        return
    end

    attempt = tonumber(attempt or 1) or 1
    local ok = select(1, M._cache_formats_from_current_playback('poll-' .. tostring(attempt)))
    if ok then
        return
    end

    if attempt >= 12 then
        _lua_log('formats: playback cache poll exhausted for url=' .. url)
        return
    end

    mp.add_timeout(0.5, function()
        M._schedule_playback_format_cache_poll(url, generation, attempt + 1)
    end)
end

function M.FileState:fetch_formats(cb)
    local url = tostring(self.url or '')
    _lua_log('fetch-formats: started for url=' .. url)
    if url == '' or not _is_http_url(url) then
        _lua_log('fetch-formats: skipped (not a url)')
        if cb then cb(false, 'not a url') end
        return
    end

    if _extract_store_hash(url) then
        _lua_log('fetch-formats: skipped (store-hash)')
        if cb then cb(false, 'store-hash url') end
        return
    end

    if not _is_ytdlp_url(url) then
        _lua_log('fetch-formats: skipped (yt-dlp unsupported)')
        if cb then cb(false, 'yt-dlp unsupported url') end
        return
    end

    local cached = M._get_cached_formats_table(url)
    if type(cached) == 'table' then
        _lua_log('fetch-formats: using cached table')
        self:set_formats(url, cached)
        if cb then cb(true, nil) end
        return
    end

    local raw_cached, raw_err = M._cache_formats_from_raw_info(url, nil, 'fetch')
    if type(raw_cached) == 'table' then
        self:set_formats(url, raw_cached)
        _lua_log('fetch-formats: satisfied directly from ytdl-raw-info')
        if cb then cb(true, nil) end
        return
    end
    if raw_err and raw_err ~= '' then
        _lua_log('fetch-formats: ytdl-raw-info unavailable reason=' .. tostring(raw_err))
    end

    local function _perform_request()
        if _formats_inflight[url] then
            _lua_log('fetch-formats: already inflight, adding waiter')
            _formats_waiters[url] = _formats_waiters[url] or {}
            if cb then table.insert(_formats_waiters[url], cb) end
            return
        end
        _lua_log('fetch-formats: initiating subprocess probe')
        _formats_inflight[url] = true
        _formats_waiters[url] = _formats_waiters[url] or {}
        if cb then table.insert(_formats_waiters[url], cb) end

        M._run_formats_probe_async(url, function(resp, err)
            _lua_log('fetch-formats: subprocess callback received err=' .. tostring(err))
            _formats_inflight[url] = nil

            local ok = false
            local reason = err
            if resp and resp.success and type(resp.table) == 'table' then
                ok = true
                reason = nil
                self:set_formats(url, resp.table)
                M._cache_formats_for_url(url, resp.table)
                _lua_log('formats: cached ' .. tostring((resp.table.rows and #resp.table.rows) or 0) .. ' rows for url via subprocess probe')
            else
                _lua_log('fetch-formats: request failed success=' .. tostring(resp and resp.success))
                if type(resp) == 'table' then
                    if resp.error and tostring(resp.error) ~= '' then
                        reason = tostring(resp.error)
                    elseif resp.stderr and tostring(resp.stderr) ~= '' then
                        reason = tostring(resp.stderr)
                    end
                end
            end

            local waiters = _formats_waiters[url] or {}
            _lua_log('fetch-formats: calling ' .. tostring(#waiters) .. ' waiters with ok=' .. tostring(ok) .. ' reason=' .. tostring(reason))
            _formats_waiters[url] = nil
            for _, fn in ipairs(waiters) do
                pcall(fn, ok, reason)
            end
        end)
    end

    _perform_request()
end

function M._prefetch_formats_for_url(url, attempt)
    url = tostring(url or '')
    if url == '' or not _is_http_url(url) then
        return
    end
    if not _is_ytdlp_url(url) then
        _formats_prefetch_retries[url] = nil
        _lua_log('prefetch-formats: skipped (yt-dlp unsupported) url=' .. url)
        return
    end
    attempt = tonumber(attempt or 1) or 1

    local cached = M._get_cached_formats_table(url)
    if type(cached) == 'table' and type(cached.rows) == 'table' and #cached.rows > 0 then
        _formats_prefetch_retries[url] = nil
        return
    end

    local raw_cached = nil
    raw_cached = select(1, M._cache_formats_from_raw_info(url, nil, 'prefetch-' .. tostring(attempt)))
    if type(raw_cached) == 'table' and type(raw_cached.rows) == 'table' and #raw_cached.rows > 0 then
        _formats_prefetch_retries[url] = nil
        _lua_log('prefetch-formats: satisfied directly from ytdl-raw-info on attempt=' .. tostring(attempt))
        return
    end

    _set_current_web_url(url)
    if type(M.file) == 'table' then
        if M.file.set_url then
            M.file:set_url(url)
        else
            M.file.url = url
        end
        if M.file.fetch_formats then
            M.file:fetch_formats(function(ok, err)
                if ok then
                    _formats_prefetch_retries[url] = nil
                    _lua_log('prefetch-formats: cached formats for url on attempt=' .. tostring(attempt))
                    return
                end

                local reason = tostring(err or '')
                local retryable = reason == 'helper not running'
                    or reason == 'helper not ready'
                    or reason:find('timeout waiting response', 1, true) ~= nil
                if not retryable then
                    _formats_prefetch_retries[url] = nil
                    _lua_log('prefetch-formats: giving up for url reason=' .. reason)
                    return
                end

                local delays = { 1.5, 4.0, 8.0 }
                if attempt > #delays then
                    _formats_prefetch_retries[url] = nil
                    _lua_log('prefetch-formats: retries exhausted for url reason=' .. reason)
                    return
                end

                local next_attempt = attempt + 1
                if (_formats_prefetch_retries[url] or 0) >= next_attempt then
                    return
                end
                _formats_prefetch_retries[url] = next_attempt
                local delay = delays[attempt]
                _lua_log('prefetch-formats: scheduling retry attempt=' .. tostring(next_attempt) .. ' delay=' .. tostring(delay) .. 's reason=' .. reason)
                mp.add_timeout(delay, function()
                    if type(M._get_cached_formats_table(url)) == 'table' then
                        _formats_prefetch_retries[url] = nil
                        return
                    end
                    M._prefetch_formats_for_url(url, next_attempt)
                end)
            end)
        end
    end
end

function M._open_loading_formats_menu(title)
    _uosc_open_list_picker(DOWNLOAD_FORMAT_MENU_TYPE, title or 'Pick format', {
        {
            title = 'Loading formats…',
            hint = 'Fetching format list',
            value = { 'script-message-to', mp.get_script_name(), 'medios-nop', '{}' },
        },
    })
end

function M._debug_dump_formatted_formats(url, tbl, items)
    local row_count = 0
    if type(tbl) == 'table' and type(tbl.rows) == 'table' then
        row_count = #tbl.rows
    end
    local item_count = 0
    if type(items) == 'table' then
        item_count = #items
    end

    _lua_log('formats-dump: url=' .. tostring(url or '') .. ' rows=' .. tostring(row_count) .. ' menu_items=' .. tostring(item_count))

    -- Dump the formatted picker items (first 30) so we can confirm the
    -- list is being built and looks sane.
    if type(items) == 'table' then
        local limit = 30
        for i = 1, math.min(#items, limit) do
            local it = items[i] or {}
            local title = tostring(it.title or '')
            local hint = tostring(it.hint or '')
            _lua_log('formats-item[' .. tostring(i) .. ']: ' .. title .. (hint ~= '' and (' | ' .. hint) or ''))
        end
        if #items > limit then
            _lua_log('formats-dump: (truncated; total=' .. tostring(#items) .. ')')
        end
    end
end

function M._show_format_list_osd(items, max_items)
    if type(items) ~= 'table' then
        return
    end
    local total = #items
    if total == 0 then
        mp.osd_message('No formats available', 4)
        return
    end
    local limit = max_items or 8
    if limit < 1 then
        limit = 1
    end
    local lines = {}
    for i = 1, math.min(total, limit) do
        local it = items[i] or {}
        local title = tostring(it.title or '')
        local hint = tostring(it.hint or '')
        if hint ~= '' then
            lines[#lines + 1] = title .. ' — ' .. hint
        else
            lines[#lines + 1] = title
        end
    end
    if total > limit then
        lines[#lines + 1] = '… +' .. tostring(total - limit) .. ' more'
    end
    if #lines > 0 then
        mp.osd_message(table.concat(lines, '\n'), 6)
    end
end

function M._current_ytdl_format_string()
    local url = _current_url_for_web_actions() or _current_target() or ''
    local raw = mp.get_property_native('ytdl-raw-info')

    -- Preferred: mpv exposes the active ytdl format string.
    local fmt = trim(tostring(mp.get_property_native('ytdl-format') or ''))
    local suspicious_reason = M._suspicious_ytdl_format_reason(fmt, url, raw)
    if fmt ~= '' and not suspicious_reason then
        M._remember_ytdl_download_format(url, fmt)
        return fmt
    elseif fmt ~= '' then
        _lua_log('ytdl-format: ignoring suspicious current format source=ytdl-format value=' .. tostring(fmt) .. ' reason=' .. tostring(suspicious_reason))
    end

    -- Fallbacks: option value, or raw info if available.
    local opt = trim(tostring(mp.get_property('options/ytdl-format') or ''))
    suspicious_reason = M._suspicious_ytdl_format_reason(opt, url, raw)
    if opt ~= '' and not suspicious_reason then
        M._remember_ytdl_download_format(url, opt)
        return opt
    elseif opt ~= '' then
        _lua_log('ytdl-format: ignoring suspicious current format source=options/ytdl-format value=' .. tostring(opt) .. ' reason=' .. tostring(suspicious_reason))
    end

    if type(raw) == 'table' then
        if raw.format_id and tostring(raw.format_id) ~= '' then
            local raw_format_id = tostring(raw.format_id)
            suspicious_reason = M._suspicious_ytdl_format_reason(raw_format_id, url, raw)
            if not suspicious_reason then
                M._remember_ytdl_download_format(url, raw_format_id)
                return raw_format_id
            end
            _lua_log('ytdl-format: ignoring suspicious current format source=ytdl-raw-info.format_id value=' .. tostring(raw_format_id) .. ' reason=' .. tostring(suspicious_reason))
        end
        local rf = raw.requested_formats
        if type(rf) == 'table' then
            local parts = {}
            for _, item in ipairs(rf) do
                if type(item) == 'table' and item.format_id and tostring(item.format_id) ~= '' then
                    parts[#parts + 1] = tostring(item.format_id)
                end
            end
            if #parts >= 1 then
                local joined = table.concat(parts, '+')
                suspicious_reason = M._suspicious_ytdl_format_reason(joined, url, raw)
                if not suspicious_reason then
                    M._remember_ytdl_download_format(url, joined)
                    return joined
                end
                _lua_log('ytdl-format: ignoring suspicious current format source=ytdl-raw-info.requested_formats value=' .. tostring(joined) .. ' reason=' .. tostring(suspicious_reason))
            end
        end
    end

    if url ~= '' and _is_ytdlp_url(url) then
        local remembered = M._get_remembered_ytdl_download_format(url)
        suspicious_reason = M._suspicious_ytdl_format_reason(remembered, url, raw)
        if remembered and not suspicious_reason then
            _lua_log('ytdl-format: using remembered download fallback value=' .. tostring(remembered) .. ' url=' .. tostring(url))
            return remembered
        elseif remembered then
            _lua_log('ytdl-format: ignoring suspicious remembered fallback value=' .. tostring(remembered) .. ' reason=' .. tostring(suspicious_reason))
        end
    end

    return nil
end

function M._resolve_cli_entrypoint()
    local configured = trim(tostring((opts and opts.cli_path) or ''))
    if configured ~= '' and configured ~= 'CLI.py' then
        if configured:match('[/\\]') then
            if _path_exists(configured) then
                return configured
            end
        else
            return configured
        end
    end

    local repo_root = _detect_repo_root()
    if repo_root ~= '' then
        local candidate = utils.join_path(repo_root, 'CLI.py')
        if _path_exists(candidate) then
            return candidate
        end
    end

    return configured ~= '' and configured or 'CLI.py'
end

function M._build_pipeline_cli_args(pipeline_cmd, seeds)
    pipeline_cmd = trim(tostring(pipeline_cmd or ''))
    if pipeline_cmd == '' then
        return nil, 'empty pipeline command'
    end

    local python = _resolve_python_exe(true)
    if not python or python == '' then
        python = _resolve_python_exe(false)
    end
    if not python or python == '' then
        return nil, 'python not found'
    end

    local cli_path = M._resolve_cli_entrypoint()
    local args = { python, cli_path, 'pipeline', '--pipeline', pipeline_cmd }
    if seeds ~= nil then
        local seeds_json = utils.format_json(seeds)
        if type(seeds_json) == 'string' and seeds_json ~= '' then
            args[#args + 1] = '--seeds-json'
            args[#args + 1] = seeds_json
        end
    end

    return args, nil
end

function M._run_pipeline_cli_detached(pipeline_cmd, seeds)
    local args, build_err = M._build_pipeline_cli_args(pipeline_cmd, seeds)
    if type(args) ~= 'table' then
        return false, tostring(build_err or 'invalid pipeline args')
    end

    local cmd = {
        name = 'subprocess',
        args = args,
        detach = true,
    }

    local ok, result, detail = _run_subprocess_command(cmd)
    if ok then
        _lua_log('pipeline-detached: spawned via cli cmd=' .. tostring(pipeline_cmd))
        return true, detail
    end

    _lua_log('pipeline-detached: cli spawn failed detail=' .. tostring(detail or _describe_subprocess_result(result)))
    return false, detail or _describe_subprocess_result(result)
end

_run_pipeline_detached = function(pipeline_cmd, on_failure, seeds)
    if not pipeline_cmd or pipeline_cmd == '' then
        return false
    end
    local ok, detail = M._run_pipeline_cli_detached(pipeline_cmd, seeds)
    if ok then
        return true
    end

    ensure_mpv_ipc_server()
    if not ensure_pipeline_helper_running() then
        if type(on_failure) == 'function' then
            on_failure(nil, detail ~= '' and detail or 'helper not running')
        end
        return false
    end

    _run_helper_request_async({ op = 'run-detached', data = { pipeline = pipeline_cmd, seeds = seeds } }, 1.0, function(resp, err)
        if resp and resp.success then
            return
        end
        if type(on_failure) == 'function' then
            on_failure(resp, err or detail)
        end
    end)
    return true
end

_run_pipeline_background_job = function(pipeline_cmd, seeds, on_started, on_complete, timeout_seconds, poll_interval_seconds)
    pipeline_cmd = trim(tostring(pipeline_cmd or ''))
    if pipeline_cmd == '' then
        if type(on_complete) == 'function' then
            on_complete(nil, 'empty pipeline command')
        end
        return false
    end

    ensure_mpv_ipc_server()
    if not ensure_pipeline_helper_running() then
        if type(on_complete) == 'function' then
            on_complete(nil, 'helper not running')
        end
        return false
    end

    _run_helper_request_async({ op = 'run-background', data = { pipeline = pipeline_cmd, seeds = seeds } }, 8.0, function(resp, err)
        if err or not resp or not resp.success then
            if type(on_complete) == 'function' then
                on_complete(nil, err or (resp and resp.error) or 'failed to start background job')
            end
            return
        end

        local job_id = trim(tostring(resp.job_id or ''))
        if job_id == '' then
            if type(on_complete) == 'function' then
                on_complete(nil, 'missing background job id')
            end
            return
        end

        if type(on_started) == 'function' then
            on_started(job_id, resp)
        end

        local deadline = mp.get_time() + math.max(tonumber(timeout_seconds or 0) or 0, 15.0)
        local poll_interval = math.max(tonumber(poll_interval_seconds or 0) or 0, 0.25)
        local poll_inflight = false
        local timer = nil

        local function finish(job, finish_err)
            if timer then
                timer:kill()
                timer = nil
            end
            if type(on_complete) == 'function' then
                on_complete(job, finish_err)
            end
        end

        timer = mp.add_periodic_timer(poll_interval, function()
            if poll_inflight then
                return
            end
            if mp.get_time() >= deadline then
                finish(nil, 'timeout waiting background job')
                return
            end

            poll_inflight = true
            _run_helper_request_async({ op = 'job-status', data = { job_id = job_id }, quiet = true }, math.max(poll_interval + 0.75, 1.25), function(status_resp, status_err)
                poll_inflight = false

                if status_err then
                    _lua_log('background-job: poll retry job=' .. tostring(job_id) .. ' err=' .. tostring(status_err))
                    return
                end

                if not status_resp or not status_resp.success then
                    local status_error = status_resp and status_resp.error or 'job status unavailable'
                    finish(nil, status_error)
                    return
                end

                local job = status_resp.job
                local status = type(job) == 'table' and tostring(job.status or '') or tostring(status_resp.status or '')
                if status == 'success' or status == 'failed' then
                    finish(job, nil)
                end
            end)
        end)
    end)

    return true
end

function M._open_save_location_picker_for_pending_download()
    if type(_pending_download) ~= 'table' or not _pending_download.url or not _pending_download.format then
        return
    end

    _ensure_selected_store_loaded()

    local clip_range = trim(tostring(_pending_download.clip_range or ''))
    local title = clip_range ~= '' and ('Save clip ' .. clip_range) or 'Save location'

    local function build_items()
        local selected = _get_selected_store()
        local visible_names = M._visible_store_names()
        local items = {
            {
                title = 'Pick folder…',
                hint = clip_range ~= '' and ('Save clip ' .. clip_range .. ' to a local folder') or 'Save to a local folder',
                value = { 'script-message-to', mp.get_script_name(), 'medios-download-pick-path', '{}' },
            },
        }

        if #visible_names > 0 then
            for _, name in ipairs(visible_names) do
                name = trim(tostring(name or ''))
                if name ~= '' then
                    local payload = { store = name }
                    local hint = _store_status_hint_for_url(name, tostring(_pending_download.url or ''), nil)
                    if (not hint or hint == '') and selected ~= '' and name == selected then
                        hint = 'Current store'
                    end
                    items[#items + 1] = {
                        title = name,
                        hint = hint,
                        active = (selected ~= '' and name == selected) and true or false,
                        value = { 'script-message-to', mp.get_script_name(), 'medios-download-pick-store', utils.format_json(payload) },
                    }
                end
            end
        end
        return items
    end

    -- Always open immediately with whatever store cache we have.
    _uosc_open_list_picker(DOWNLOAD_STORE_MENU_TYPE, title, build_items())

    -- Best-effort refresh; if it succeeds, reopen menu with stores.
    mp.add_timeout(0.05, function()
        if type(_pending_download) ~= 'table' or not _pending_download.url or not _pending_download.format then
            return
        end
        _refresh_store_cache(1.5, function(success, changed)
            if success and changed then
                _uosc_open_list_picker(DOWNLOAD_STORE_MENU_TYPE, title, build_items())
            end
        end)
    end)
end

-- Prime store cache shortly after load (best-effort; picker also refreshes on-demand).
mp.add_timeout(0.10, function()
    pcall(_ensure_selected_store_loaded)
    if not _store_cache_loaded then
        pcall(_refresh_store_cache, 1.5)
    end
    pcall(_refresh_current_store_url_status, 'startup')
end)

function M._height_from_resolution_text(res)
    res = trim(tostring(res or ''))
    if res == '' then
        return nil
    end
    local _w, h = res:match('^(%d+)x(%d+)$')
    if h then
        return tonumber(h)
    end
    local p = res:match('^(%d+)p$')
    if p then
        return tonumber(p)
    end
    return nil
end

-- mpv's own ytdl_hook fetches formats via a separate yt-dlp invocation than our
-- picker probe, and YouTube's format list can shift between the two calls, so a
-- literal itag selector can occasionally fail with "Requested format is not
-- available". Append a resolution-capped fallback so playback still starts.
function M._format_selector_with_fallback(fmt, height)
    fmt = trim(tostring(fmt or ''))
    if fmt == '' then
        return fmt
    end
    local fallback = {}
    if height and height > 0 then
        fallback[#fallback + 1] = 'best[height<=' .. tostring(height) .. ']'
    end
    fallback[#fallback + 1] = 'best'
    return fmt .. '/' .. table.concat(fallback, '/')
end

function M._apply_ytdl_format_and_reload(url, fmt, height)
    if not url or url == '' or not fmt or fmt == '' then
        return
    end

    local suspicious_reason = M._suspicious_ytdl_format_reason(fmt, url, mp.get_property_native('ytdl-raw-info'))
    if suspicious_reason then
        pcall(mp.set_property, 'options/ytdl-format', '')
        pcall(mp.set_property, 'file-local-options/ytdl-format', '')
        pcall(mp.set_property, 'ytdl-format', '')
        _lua_log('change-format: rejected suspicious format=' .. tostring(fmt) .. ' reason=' .. tostring(suspicious_reason) .. ' url=' .. tostring(url))
        mp.osd_message('Invalid format selection', 3)
        return
    end

    local pos = mp.get_property_number('time-pos')
    local paused = mp.get_property_native('pause') and true or false
    local media_title = trim(tostring(mp.get_property('media-title') or ''))

    -- Remember the exact picked selector (no fallback) for download reuse, but
    -- apply a fallback-augmented selector for the actual playback reload.
    M._remember_ytdl_download_format(url, fmt)
    local playback_fmt = M._format_selector_with_fallback(fmt, height)

    _lua_log('change-format: setting ytdl format=' .. tostring(playback_fmt))
    _skip_next_store_check_url = _normalize_url_for_store_lookup(url)
    _set_current_web_url(url)
    if _is_ytdlp_url(url) then
        M._prime_web_subtitle_global_defaults('change-format')
        M._apply_web_subtitle_load_defaults('change-format', url)
    end
    pcall(mp.set_property, 'options/ytdl-format', tostring(playback_fmt))
    pcall(mp.set_property, 'file-local-options/ytdl-format', tostring(playback_fmt))
    pcall(mp.set_property, 'ytdl-format', tostring(playback_fmt))

    local load_options = {
        ['ytdl-format'] = tostring(playback_fmt),
    }
    if pos and pos > 0 then
        load_options['start'] = tostring(pos)
    end
    if media_title ~= '' then
        load_options['force-media-title'] = media_title
    end
    if paused then
        load_options['pause'] = 'yes'
    end

    M._close_uosc_menu_and_sync(DOWNLOAD_FORMAT_MENU_TYPE, 'change-format-reload')
    _lua_log('change-format: reloading current url with per-file options')
    mp.command_native({ 'loadfile', url, 'replace', -1, load_options })

    if paused then
        mp.set_property_native('pause', true)
    end
end

function M._start_download_flow_for_current()
    local target = _current_url_for_web_actions() or _current_target()
    if not target or target == '' then
        mp.osd_message('No current item', 2)
        return
    end

    _lua_log('download: current target=' .. tostring(target))

    local store_hash = _extract_store_hash(target)
    if store_hash then
        M._pick_folder_async(function(folder, err)
            if err and err ~= '' then
                mp.osd_message('Folder picker failed: ' .. tostring(err), 4)
                return
            end
            if not folder or folder == '' then
                return
            end

            ensure_mpv_ipc_server()
            local pipeline_cmd = 'file -download -instance ' .. quote_pipeline_arg(store_hash.store) .. ' -query ' .. quote_pipeline_arg('hash:' .. store_hash.hash) .. ' -path ' .. quote_pipeline_arg(folder)
            _queue_pipeline_in_repl(
                pipeline_cmd,
                'Queued in REPL: store copy',
                'REPL queue failed',
                'download-store-copy',
                {
                    mpv_notify = {
                        success_text = 'Copy completed: store ' .. tostring(store_hash.store),
                        failure_text = 'Copy failed: store ' .. tostring(store_hash.store),
                        duration_ms = 3500,
                    },
                }
            )
        end)
        return
    end

    -- Non-store URL flow: use the current yt-dlp-selected format and ask for save location.
    local url = tostring(target)
    local download_url, stripped_playlist = _download_url_for_current_item(url)
    if stripped_playlist then
        _lua_log('download: stripped hidden playlist params from current url -> ' .. tostring(download_url))
        url = tostring(download_url)
    end
    local fmt = M._current_ytdl_format_string()

    if not fmt or fmt == '' then
        _lua_log('download: could not determine current ytdl format string')
        mp.osd_message('Cannot determine current format; use Change Format first', 5)
        return
    end

    _lua_log('download: using current format=' .. tostring(fmt))
    local clip_range = _get_trim_range_from_clip_markers()
    _pending_download = {
        url = url,
        format = fmt,
        clip_range = clip_range,
    }

    if clip_range and clip_range ~= '' then
        _lua_log('download: using clip_range=' .. tostring(clip_range))
    else
        local clip_marker_count = 0
        local marker_label = nil
        for idx = 1, CLIP_MARKER_SLOT_COUNT do
            local marker_time = clip_markers[idx]
            if type(marker_time) == 'number' then
                clip_marker_count = clip_marker_count + 1
                if not marker_label then
                    marker_label = _format_clip_marker_label(marker_time)
                end
            end
        end
        if clip_marker_count == 1 then
        _lua_log('download: single clip marker detected; asking whether to continue with full download')
            if not ensure_uosc_loaded() then
                mp.osd_message('Only one clip marker is set. Set the second marker or clear markers before downloading.', 4)
                return
            end
            _uosc_open_list_picker('medios_download_clip_decision', marker_label and ('One clip marker set: ' .. marker_label) or 'One clip marker set', {
                {
                    title = 'Download full item',
                    hint = 'Ignore the single marker and continue',
                    value = { 'script-message-to', mp.get_script_name(), 'medios-download-proceed-full', '{}' },
                },
                {
                    title = 'Clear clip markers and download full item',
                    hint = 'Reset markers, then continue with a full download',
                    value = { 'script-message-to', mp.get_script_name(), 'medios-download-clear-markers-and-proceed', '{}' },
                },
                {
                    title = 'Go back and edit clip markers',
                    hint = 'Set the second marker or replace the existing one',
                    value = { 'script-message-to', mp.get_script_name(), 'medios-download-edit-markers', '{}' },
                },
            })
            return
        end
    end

    M._open_save_location_picker_for_pending_download()
end

mp.register_script_message('medios-download-current', function()
    M._start_download_flow_for_current()
end)

mp.register_script_message('medios-change-format-current', function()
    local target = _current_url_for_web_actions() or _current_target()
    if not target or target == '' then
        mp.osd_message('No current item', 2)
        return
    end

    local store_hash = _extract_store_hash(target)
    if store_hash then
        mp.osd_message('Change Format is only for URL playback', 4)
        return
    end

    local url = tostring(target)
    _set_current_web_url(url)

    -- Ensure file state is tracking the current URL.
    if type(M.file) == 'table' then
        if M.file.set_url then
            M.file:set_url(url)
        else
            M.file.url = url
        end
    end

    -- If formats were already prefetched for this URL, open instantly.
    local cached_tbl = nil
    if type(M.file) == 'table'
        and M.file.url == url
        and type(M.file.formats) == 'table' then
        cached_tbl = M.file.formats
    else
        cached_tbl = M._get_cached_formats_table(url)
    end
    if not (type(cached_tbl) == 'table' and type(cached_tbl.rows) == 'table' and #cached_tbl.rows > 0) then
        cached_tbl = select(1, M._cache_formats_from_raw_info(url))
    end
    if type(cached_tbl) == 'table' and type(cached_tbl.rows) == 'table' and #cached_tbl.rows > 0 then
        _pending_format_change = { url = url, token = 'cached', formats_table = cached_tbl }
        M._open_format_picker_for_table(url, cached_tbl)
        return
    end

    local token = tostring(math.floor(mp.get_time() * 1000)) .. '-' .. tostring(math.random(100000, 999999))
    _pending_format_change = { url = url, token = token }
    M._open_loading_formats_menu('Change format')

    -- Non-blocking: ask the per-file state to fetch formats in the background.
    if type(M.file) == 'table' and M.file.fetch_formats then
        _lua_log('change-format: formats not cached yet; fetching in background')
        M.file:fetch_formats(function(ok, err)
            if type(_pending_format_change) ~= 'table' or _pending_format_change.token ~= token then
                return
            end
            if not ok then
                local msg2 = tostring(err or '')
                if msg2 == '' then
                    msg2 = 'unknown'
                end
                _lua_log('change-format: formats failed: ' .. msg2)
                mp.osd_message('Failed to load format list: ' .. msg2, 7)
                _uosc_open_list_picker(DOWNLOAD_FORMAT_MENU_TYPE, 'Change format', {
                    {
                        title = 'Failed to load format list',
                        hint = msg2,
                        value = { 'script-message-to', mp.get_script_name(), 'medios-nop', '{}' },
                    },
                })
                return
            end

            local tbl = (type(M.file.formats) == 'table') and M.file.formats or M._get_cached_formats_table(url)
            if type(tbl) ~= 'table' or type(tbl.rows) ~= 'table' or #tbl.rows == 0 then
                mp.osd_message('No formats available', 4)
                return
            end

            _pending_format_change.formats_table = tbl
            M._open_format_picker_for_table(url, tbl)
        end)
    end
end)

-- Prefetch formats for yt-dlp-supported URLs on load so Change Format is instant.
mp.register_event('file-loaded', function()
    M._sync_current_web_url_from_playback()
    M._attempt_start_lyric_helper_async('file-loaded')
    M._schedule_web_subtitle_activation('file-loaded')
    _refresh_current_store_url_status('file-loaded')
    local target = _current_url_for_web_actions() or _current_target()
    if not target or target == '' then
        return
    end
    local url = tostring(target)
    if not _is_http_url(url) then
        return
    end
    mp.add_timeout(0.1, function()
        M._reset_uosc_input_state('file-loaded-web')
    end)

    _format_cache_poll_generation = _format_cache_poll_generation + 1
    if _extract_store_hash(url) or not _is_ytdlp_url(url) then
        return
    end

    local ok, err = M._cache_formats_from_current_playback('file-loaded')
    if ok then
        _lua_log('formats: file-loaded cache succeeded for url=' .. url)
    else
        _lua_log('formats: file-loaded cache pending reason=' .. tostring(err or 'unknown'))
    end
    M._schedule_playback_format_cache_poll(url, _format_cache_poll_generation, 1)
    M._prefetch_formats_for_url(url)
end)

mp.observe_property('track-list', 'native', function()
    M._ensure_current_subtitles_visible('observe-track-list')
end)

mp.observe_property('window-minimized', 'bool', function(_name, value)
    if value == true then
        _lua_log('window: minimized -> reset uosc input state')
        M._reset_uosc_input_state('window-minimized')
        return
    end

    _lua_log('window: restored -> reset uosc input state')
    M._reset_uosc_input_state('window-restored')
    M._schedule_uosc_cursor_resync('window-restored')
    mp.add_timeout(0.10, function()
        M._reset_uosc_input_state('window-restored@0.10')
    end)
    mp.add_timeout(0.35, function()
        M._schedule_uosc_cursor_resync('window-restored@0.35')
    end)
end)

mp.observe_property('ytdl-raw-info', 'native', function(_name, value)
    if type(value) ~= 'table' then
        return
    end
    local ok, err = M._cache_formats_from_current_playback('observe-ytdl-raw-info', value)
    if not ok and err and err ~= '' then
        _lua_log('formats: observe-ytdl-raw-info pending reason=' .. tostring(err))
    end
end)

mp.register_script_message('medios-change-format-pick', function(json)
    if type(_pending_format_change) ~= 'table' or not _pending_format_change.url then
        return
    end
    local ok, ev = pcall(utils.parse_json, json)
    if not ok or type(ev) ~= 'table' then
        return
    end
    local idx = tonumber(ev.index or 0) or 0
    if idx <= 0 then
        return
    end
    local tbl = _pending_format_change.formats_table
    if type(tbl) ~= 'table' or type(tbl.rows) ~= 'table' or not tbl.rows[idx] then
        return
    end
    local row = tbl.rows[idx]
    local sel = row.selection_args
    local fmt = nil
    if type(sel) == 'table' then
        for i = 1, #sel do
            if tostring(sel[i]) == '-format' and sel[i + 1] then
                fmt = tostring(sel[i + 1])
                break
            end
        end
    end
    if not fmt or fmt == '' then
        mp.osd_message('Invalid format selection', 3)
        return
    end

    local resolution_text = nil
    if type(row.columns) == 'table' then
        for _, col in ipairs(row.columns) do
            if type(col) == 'table' and col.name == 'Resolution' then
                resolution_text = col.value
                break
            end
        end
    end
    local height = M._height_from_resolution_text(resolution_text)

    local url = tostring(_pending_format_change.url)
    _pending_format_change = nil
    M._apply_ytdl_format_and_reload(url, fmt, height)
end)

mp.register_script_message('medios-download-pick-store', function(json)
    if type(_pending_download) ~= 'table' or not _pending_download.url or not _pending_download.format then
        return
    end
    local ok, ev = pcall(utils.parse_json, json)
    if not ok or type(ev) ~= 'table' then
        return
    end
    local store = trim(tostring(ev.store or ''))
    if store == '' then
        return
    end

    local url = tostring(_pending_download.url)
    local fmt = tostring(_pending_download.format)
    local clip_range = trim(tostring(_pending_download.clip_range or ''))
    local query = 'format:' .. fmt
    if clip_range ~= '' then
        query = query .. ',clip:' .. clip_range
    end
    local clip_suffix = clip_range ~= '' and (' [' .. clip_range .. ']') or ''

    local pipeline_cmd = 'file -download -url ' .. quote_pipeline_arg(url)
        .. ' -query ' .. quote_pipeline_arg(query)
        .. ' | file -add -plugin hydrusnetwork -instance ' .. quote_pipeline_arg(store)

    _set_selected_store(store)
    _queue_pipeline_in_repl(
        pipeline_cmd,
        'Queued in REPL: save to store ' .. store,
        'REPL queue failed',
        'download-store-save',
        {
            mpv_notify = {
                success_text = 'Download completed: store ' .. store .. ' [' .. fmt .. ']' .. clip_suffix,
                failure_text = 'Download failed: store ' .. store .. ' [' .. fmt .. ']' .. clip_suffix,
                duration_ms = 3500,
            },
        }
    )
    _pending_download = nil
end)

mp.register_script_message('medios-download-proceed-full', function()
    if type(_pending_download) ~= 'table' or not _pending_download.url or not _pending_download.format then
        return
    end
    _pending_download.clip_range = nil
    M._open_save_location_picker_for_pending_download()
end)

mp.register_script_message('medios-download-clear-markers-and-proceed', function()
    if type(_pending_download) ~= 'table' or not _pending_download.url or not _pending_download.format then
        return
    end
    _reset_clip_markers()
    _pending_download.clip_range = nil
    M._open_save_location_picker_for_pending_download()
end)

mp.register_script_message('medios-download-edit-markers', function()
    _pending_download = nil
    mp.osd_message('Edit clip markers, then run Download again', 4)
end)

mp.register_script_message('medios-download-pick-path', function()
    if type(_pending_download) ~= 'table' or not _pending_download.url or not _pending_download.format then
        return
    end

    M._pick_folder_async(function(folder, err)
        if err and err ~= '' then
            mp.osd_message('Folder picker failed: ' .. tostring(err), 4)
            return
        end
        if not folder or folder == '' then
            return
        end

        local url = tostring(_pending_download.url)
        local fmt = tostring(_pending_download.format)
        local clip_range = trim(tostring(_pending_download.clip_range or ''))
        local query = 'format:' .. fmt
        if clip_range ~= '' then
            query = query .. ',clip:' .. clip_range
        end
        local clip_suffix = clip_range ~= '' and (' [' .. clip_range .. ']') or ''

        local pipeline_cmd = 'file -download -url ' .. quote_pipeline_arg(url)
            .. ' -query ' .. quote_pipeline_arg(query)
            .. ' | file -add -plugin local -instance ' .. quote_pipeline_arg(folder)

        _queue_pipeline_in_repl(
            pipeline_cmd,
            'Queued in REPL: save to folder',
            'REPL queue failed',
            'download-folder-save',
            {
                mpv_notify = {
                    success_text = 'Download completed: folder [' .. fmt .. ']' .. clip_suffix,
                    failure_text = 'Download failed: folder [' .. fmt .. ']' .. clip_suffix,
                    duration_ms = 3500,
                },
            }
        )
        _pending_download = nil
    end)
end)

local function run_pipeline_via_ipc(pipeline_cmd, seeds, timeout_seconds)
    if not ensure_pipeline_helper_running() then
        return nil
    end

    -- Avoid a race where we send the request before the helper has connected
    -- and installed its property observer, which would cause a timeout and
    -- force a noisy CLI fallback.
    do
        local deadline = mp.get_time() + 1.0
        while mp.get_time() < deadline do
            if _is_pipeline_helper_ready() then
                break
            end
            mp.wait_event(0.05)
        end
        if not _is_pipeline_helper_ready() then
            _pipeline_helper_started = false
            return nil
        end
    end

    local id = tostring(math.floor(mp.get_time() * 1000)) .. '-' .. tostring(math.random(100000, 999999))
    local req = { id = id, pipeline = pipeline_cmd }
    if seeds then
        req.seeds = seeds
    end

    -- Clear any previous response to reduce chances of reading stale data.
    mp.set_property(PIPELINE_RESP_PROP, '')
    mp.set_property(PIPELINE_REQ_PROP, utils.format_json(req))

    local deadline = mp.get_time() + (timeout_seconds or 5)
    while mp.get_time() < deadline do
        local resp_json = mp.get_property(PIPELINE_RESP_PROP)
        if resp_json and resp_json ~= '' then
            local ok, resp = pcall(utils.parse_json, resp_json)
            if ok and resp and resp.id == id then
                if resp.success then
                    return resp.stdout or ''
                end
                local details = ''
                if resp.error and tostring(resp.error) ~= '' then
                    details = tostring(resp.error)
                end
                if resp.stderr and tostring(resp.stderr) ~= '' then
                    if details ~= '' then
                        details = details .. "\n"
                    end
                    details = details .. tostring(resp.stderr)
                end
                local log_path = resp.log_path
                if log_path and tostring(log_path) ~= '' then
                    details = (details ~= '' and (details .. "\n") or '') .. 'Log: ' .. tostring(log_path)
                end
                return nil, (details ~= '' and details or 'unknown')
            end
        end
        mp.wait_event(0.05)
    end
    -- Helper may have crashed or never started; allow retry on next call.
    _pipeline_helper_started = false
    return nil
end

-- Detect CLI path
local function detect_script_dir()
    local dir = mp.get_script_directory()
    if dir and dir ~= "" then return dir end

    -- Fallback to debug info path
    local src = debug.getinfo(1, "S").source
    if src and src:sub(1, 1) == "@" then
        local path = src:sub(2)
        local parent = path:match("(.*)[/\\]")
        if parent and parent ~= "" then
            return parent
        end
    end

    -- Fallback to working directory
    local cwd = utils.getcwd()
    if cwd and cwd ~= "" then return cwd end
    return nil
end

local script_dir = detect_script_dir() or ""
if not opts.cli_path then
    -- Try to locate CLI.py by walking up from this script directory.
    -- Typical layout here is: <repo>/MPV/LUA/main.lua, and <repo>/CLI.py
    opts.cli_path = find_file_upwards(script_dir, "CLI.py", 6) or "CLI.py"
end

-- Ref: mpv_lua_api.py was removed in favor of pipeline_helper (run_pipeline_via_ipc_response).
-- This placeholder comment ensures we don't have code shifting issues.

-- Run a Medeia pipeline command via the Python pipeline helper (IPC request/response).
-- Calls the callback with stdout on success or error message on failure.
function M.run_pipeline(pipeline_cmd, seeds, cb)
    _lua_log('M.run_pipeline called with cmd: ' .. tostring(pipeline_cmd))
    cb = cb or function() end
    pipeline_cmd = trim(tostring(pipeline_cmd or ''))
    if pipeline_cmd == '' then
        _lua_log('M.run_pipeline: empty command')
        cb(nil, 'empty pipeline command')
        return
    end
    ensure_mpv_ipc_server()

    -- Use a longer timeout for `.mpv -url` commands to avoid races with slow helper starts.
    local lower_cmd = pipeline_cmd:lower()
    local is_mpv_load = lower_cmd:match('%.mpv%s+%-url') ~= nil
    local timeout_seconds = is_mpv_load and 45 or 30
    _run_pipeline_request_async(pipeline_cmd, seeds, timeout_seconds, function(resp, err)
        _lua_log('M.run_pipeline callback fired: resp=' .. tostring(resp) .. ', err=' .. tostring(err))
        if resp and resp.success then
            _lua_log('M.run_pipeline: success')
            cb(resp.stdout or '', nil)
            return
        end
        local details = err or ''
        if details == '' and type(resp) == 'table' then
            if resp.error and tostring(resp.error) ~= '' then
                details = tostring(resp.error)
            elseif resp.stderr and tostring(resp.stderr) ~= '' then
                details = tostring(resp.stderr)
            end
        end
        if details == '' then
            details = 'unknown'
        end
        _lua_log('pipeline failed cmd=' .. tostring(pipeline_cmd) .. ' err=' .. details)
        cb(nil, details)
    end)
end

-- Helper to run pipeline and parse JSON output
function M.run_pipeline_json(pipeline_cmd, seeds, cb)
    cb = cb or function() end
    pipeline_cmd = trim(tostring(pipeline_cmd or ''))
    if pipeline_cmd == '' then
        cb(nil, 'empty pipeline command')
        return
    end

    ensure_mpv_ipc_server()

    local lower_cmd = pipeline_cmd:lower()
    local is_mpv_load = lower_cmd:match('%.mpv%s+%-url') ~= nil
    local timeout_seconds = is_mpv_load and 45 or 30
    _run_pipeline_request_async(pipeline_cmd, seeds, timeout_seconds, function(resp, err)
        if resp and resp.success then
            if type(resp.data) == 'table' then
                cb(resp.data, nil)
                return
            end

            local output = trim(tostring(resp.stdout or ''))
            if output ~= '' then
                local ok, data = pcall(utils.parse_json, output)
                if ok then
                    cb(data, nil)
                    return
                end
                _lua_log('Failed to parse JSON: ' .. output)
                cb(nil, 'malformed JSON response')
                return
            end

            cb({}, nil)
            return
        end

        local details = err or ''
        if details == '' and type(resp) == 'table' then
            if resp.error and tostring(resp.error) ~= '' then
                details = tostring(resp.error)
            elseif resp.stderr and tostring(resp.stderr) ~= '' then
                details = tostring(resp.stderr)
            end
        end
        cb(nil, details ~= '' and details or 'unknown')
    end)
end

-- Command: Get info for current file
function M.get_file_info()
    local path = mp.get_property('path')
    if not path then
        return
    end

    local seed = {{path = path}}

    M.run_pipeline_json('get-metadata', seed, function(data, err)
        if data then
            _lua_log('Metadata: ' .. utils.format_json(data))
            mp.osd_message('Metadata loaded (check console)', 3)
            return
        end
        if err then
            mp.osd_message('Failed to load metadata: ' .. tostring(err), 3)
        end
    end)
end

-- Command: Delete current file
function M.delete_current_file()
    local path = mp.get_property('path')
    if not path then
        return
    end

    local seed = {{path = path}}

    M.run_pipeline('file -delete', seed, function(_, err)
        if err then
            mp.osd_message('Delete failed: ' .. tostring(err), 3)
            return
        end
        mp.osd_message('File deleted', 3)
        mp.command('playlist-next')
    end)
end

-- Command: Load a URL via pipeline (Ctrl+Enter in prompt)
function M.open_load_url_prompt()
    _lua_log('open_load_url_prompt called')
    local menu_data = {
        type = LOAD_URL_MENU_TYPE,
        title = 'Load URL',
        search_style = 'palette',
        search_debounce = 'submit',
        on_search = 'callback',
        footnote = 'Paste/type URL, then Ctrl+Enter to load.',
        callback = {mp.get_script_name(), 'medios-load-url-event'},
        items = {},
    }

    if M._open_uosc_menu(menu_data, 'load-url-prompt') then
        _lua_log('open_load_url_prompt: sending menu to uosc')
    else
        _lua_log('menu: uosc not available; cannot open-menu for load-url')
    end
end

-- Open the command submenu with tailored cmdlets (screenshot, clip, trim prompt)
function M.open_cmd_menu()
    local screenshot_available, screenshot_kind = M._has_visual_screenshot_source()
    local screenshot_hint = screenshot_available and ((screenshot_kind == 'image') and 'Capture current image' or 'Capture current frame') or 'Disabled: no video or image frame'
    local items = {
        {
            title = 'Screenshot',
            hint = screenshot_hint,
            selectable = screenshot_available,
            value = { 'script-message-to', mp.get_script_name(), 'medios-cmd-exec', utils.format_json({ cmd = 'screenshot' }) },
        },
        {
            title = 'Capture clip marker',
            hint = 'Place a clip marker at current time',
            value = { 'script-message-to', mp.get_script_name(), 'medios-cmd-exec', utils.format_json({ cmd = 'clip' }) },
        },
        {
            title = 'Trim file',
            hint = 'Trim current file (prompt for range)',
            value = { 'script-message-to', mp.get_script_name(), 'medios-cmd-exec', utils.format_json({ cmd = 'trim' }) },
        },
    }

    local menu_data = {
        type = CMD_MENU_TYPE,
        title = 'Cmd',
        search_style = 'palette',
        search_debounce = 'submit',
        footnote = 'Type to filter or pick a command',
        items = items,
    }

    if not M._open_uosc_menu(menu_data, 'cmd-menu') then
        _lua_log('menu: uosc not available; cannot open cmd menu')
    end
end

-- Prompt for trim range via an input box and callback
local function _start_trim_with_range(range)
    _lua_log('=== TRIM START: range=' .. tostring(range))
    mp.osd_message('Trimming...', 10)
    
    local trim_module = nil
    local trim_path = nil
    local load_err = nil
    local ok_trim = false
    
    ok_trim, trim_module, trim_path, load_err = _load_lua_chunk_from_candidates('trim', 'trim.lua')

    if not trim_module or not trim_module.trim_file then
        mp.osd_message('ERROR: Could not load trim module from any path', 3)
        _lua_log('trim: FAILED - all paths exhausted, last error=' .. tostring(load_err))
        return
    end

    if ok_trim and trim_path then
        _lua_log('trim: using module at ' .. tostring(trim_path))
    end
    
    range = trim(tostring(range or ''))
    _lua_log('trim: after_trim range=' .. tostring(range))
    
    if range == '' then
        mp.osd_message('Trim cancelled (no range provided)', 3)
        _lua_log('trim: CANCELLED - empty range')
        return
    end

    local target = _current_target()
    if not target or target == '' then
        mp.osd_message('No file to trim', 3)
        _lua_log('trim: FAILED - no target')
        return
    end
    _lua_log('trim: target=' .. tostring(target))

    local store_hash = _extract_store_hash(target)
    if store_hash then
        _lua_log('trim: store_hash detected store=' .. tostring(store_hash.store) .. ' hash=' .. tostring(store_hash.hash))
    else
        _lua_log('trim: store_hash=nil (local file)')
    end
    
    -- Get the selected store (this reads from saved config or mpv property)
    _ensure_selected_store_loaded()
    local selected_store = _get_selected_store()
    -- Strip any existing quotes from the store name
    selected_store = selected_store:gsub('^"', ''):gsub('"$', '')
    _lua_log('trim: selected_store=' .. tostring(selected_store or 'NONE'))
    _lua_log('trim: _cached_store_names=' .. tostring(_cached_store_names and #_cached_store_names or 0))
    _lua_log('trim: _selected_store_index=' .. tostring(_selected_store_index or 'nil'))

    local stream = trim(tostring(mp.get_property('stream-open-filename') or ''))
    if stream == '' then
        stream = tostring(target)
    end
    _lua_log('trim: stream=' .. tostring(stream))

    local title = trim(tostring(mp.get_property('media-title') or ''))
    if title == '' then
        title = 'clip'
    end
    _lua_log('trim: title=' .. tostring(title))

    -- ===== TRIM IN LUA USING FFMPEG =====
    mp.osd_message('Starting FFmpeg trim...', 1)
    _lua_log('trim: calling trim_module.trim_file with range=' .. range)
    
    -- Get temp directory from config or use default
    local temp_dir = mp.get_property('user-data/medeia-config-temp') or os.getenv('TEMP') or os.getenv('TMP') or '/tmp'
    _lua_log('trim: using temp_dir=' .. temp_dir)
    
    local success, output_path, error_msg = trim_module.trim_file(stream, range, temp_dir)
    
    if not success then
        mp.osd_message('Trim failed: ' .. error_msg, 3)
        _lua_log('trim: FAILED - ' .. error_msg)
        return
    end
    
    _lua_log('trim: FFmpeg SUCCESS - output_path=' .. output_path)
    mp.osd_message('Trim complete, uploading...', 2)
    
    -- ===== UPLOAD TO PYTHON FOR STORAGE AND METADATA =====
    local pipeline_cmd = nil
    _lua_log('trim: === BUILDING UPLOAD PIPELINE ===')
    _lua_log('trim: store_hash=' .. tostring(store_hash and (store_hash.store .. '/' .. store_hash.hash) or 'nil'))
    _lua_log('trim: selected_store=' .. tostring(selected_store or 'nil'))
    
    if store_hash then
        -- Original file is from a store - set relationship to it
        _lua_log('trim: building store file pipeline (original from store)')
        if selected_store then
            pipeline_cmd =
                'tag -get -emit -store ' .. quote_pipeline_arg(store_hash.store) ..
                ' -query ' .. quote_pipeline_arg('hash:' .. store_hash.hash) ..
                ' | file -add -plugin hydrusnetwork -instance ' .. quote_pipeline_arg(selected_store) ..
                ' ' .. quote_pipeline_arg(output_path) ..
                ' | add-relationship -store "' .. selected_store .. '"' ..
                ' -to-hash ' .. quote_pipeline_arg(store_hash.hash)
        else
            pipeline_cmd =
                'tag -get -emit -store ' .. quote_pipeline_arg(store_hash.store) ..
                ' -query ' .. quote_pipeline_arg('hash:' .. store_hash.hash) ..
                ' | file -add -plugin hydrusnetwork -instance ' .. quote_pipeline_arg(store_hash.store) ..
                ' ' .. quote_pipeline_arg(output_path) ..
                ' | add-relationship -store "' .. store_hash.store .. '"' ..
                ' -to-hash ' .. quote_pipeline_arg(store_hash.hash)
        end
    else
        -- Local file: save to selected store if available
        _lua_log('trim: local file pipeline (not from store)')
        if selected_store then
            _lua_log('trim: building file -add command to selected_store=' .. selected_store)
            -- Don't add title if empty - the file path will be used as title by default
            pipeline_cmd = 'file -add -plugin hydrusnetwork -instance ' .. quote_pipeline_arg(selected_store) ..
                ' ' .. quote_pipeline_arg(output_path)
            _lua_log('trim: pipeline_cmd=' .. pipeline_cmd)
        else
            mp.osd_message('Trim complete: ' .. output_path, 5)
            _lua_log('trim: no store selected, trim complete at ' .. output_path)
            return
        end
    end

    if not pipeline_cmd or pipeline_cmd == '' then
        mp.osd_message('Trim error: could not build upload command', 3)
        _lua_log('trim: FAILED - empty pipeline_cmd')
        return
    end

    _lua_log('trim: final upload_cmd=' .. pipeline_cmd)
    _lua_log('trim: === CALLING PIPELINE HELPER FOR UPLOAD ===')
    
    run_pipeline_via_ipc_async(pipeline_cmd, nil, 60, function(response, err)
        if not response then
            response = { success = false, error = err or 'Timeout or IPC error' }
        end

        _lua_log('trim: api response success=' .. tostring(response.success))
        _lua_log('trim: api response error=' .. tostring(response.error or 'nil'))
        _lua_log('trim: api response stderr=' .. tostring(response.stderr or 'nil'))
        _lua_log('trim: api response returncode=' .. tostring(response.returncode or 'nil'))

        if response.stderr and response.stderr ~= '' then
            _lua_log('trim: STDERR OUTPUT: ' .. response.stderr)
        end

        if response.success then
            local msg = 'Trim and upload completed'
            if selected_store then
                msg = msg .. ' (store: ' .. selected_store .. ')'
            end
            mp.osd_message(msg, 5)
            _lua_log('trim: SUCCESS - ' .. msg)
        else
            local err_msg = err or response.error or response.stderr or 'unknown error'
            mp.osd_message('Upload failed: ' .. err_msg, 5)
            _lua_log('trim: upload FAILED - ' .. err_msg)
        end
    end)
end

function M.open_trim_prompt()
    _lua_log('=== OPEN_TRIM_PROMPT called')
    
    local marker_range = _get_trim_range_from_clip_markers()
    _lua_log('trim_prompt: marker_range=' .. tostring(marker_range or 'NONE'))
    
    if marker_range then
        _lua_log('trim_prompt: using auto-detected markers, starting trim')
        mp.osd_message('Using clip markers: ' .. marker_range, 2)
        _start_trim_with_range(marker_range)
        return
    end

    _lua_log('trim_prompt: no clip markers detected, showing prompt')
    mp.osd_message('Set 2 clip markers with the marker button, or enter range manually', 3)
    
    local selected_store = _cached_store_names and #_cached_store_names > 0 and _selected_store_index 
        and _cached_store_names[_selected_store_index] or nil
    local store_hint = selected_store and ' (saving to: ' .. selected_store .. ')' or ' (no store selected; will save locally)'

    local menu_data = {
        type = TRIM_PROMPT_MENU_TYPE,
        title = 'Trim file',
        search_style = 'palette',
        search_debounce = 'submit',
        on_search = 'callback',
        footnote = "Enter time range (e.g. '00:03:45-00:03:55' or '1h3m-1h10m30s') and press Enter" .. store_hint,
        callback = { mp.get_script_name(), 'medios-trim-run' },
        items = {
            {
                title = 'Enter range...',
                hint = 'Type range and press Enter',
                value = { 'script-message-to', mp.get_script_name(), 'medios-trim-run' },
            }
        }
    }

    if not M._open_uosc_menu(menu_data, 'trim-prompt') then
        _lua_log('menu: uosc not available; cannot open trim prompt')
    end
end

-- Handlers for the command submenu
mp.register_script_message('medios-open-cmd', function()
    M.open_cmd_menu()
end)

mp.register_script_message('medios-cmd-exec', function(json)
    local ok, ev = pcall(utils.parse_json, json)
    if not ok or type(ev) ~= 'table' then
        return
    end
    local cmd = trim(tostring(ev.cmd or ''))
    if cmd == 'screenshot' then
        _capture_screenshot()
    elseif cmd == 'clip' then
        _capture_clip()
    elseif cmd == 'trim' then
        M.open_trim_prompt()
    else
        mp.osd_message('Unknown cmd ' .. tostring(cmd), 2)
    end
end)

mp.register_script_message('medios-trim-run', function(json)
    local ok, ev = pcall(utils.parse_json, json)
    local range = nil
    if ok and type(ev) == 'table' then
        if ev.type == 'search' then
            range = trim(tostring(ev.query or ''))
        end
    end
    _start_trim_with_range(range)
end)

mp.register_script_message('medios-load-url', function()
    _lua_log('medios-load-url handler called')
    _lua_log('medios-load-url: resetting uosc input state before opening Load URL prompt')
    M._reset_uosc_input_state('medios-load-url')
    M.open_load_url_prompt()
end)

mp.register_script_message('medios-start-helper', function()
    -- Asynchronously start the pipeline helper without blocking the menu.
    attempt_start_pipeline_helper_async(function(success)
        if success then
            mp.osd_message('Pipeline helper started', 2)
        else
            mp.osd_message('Failed to start pipeline helper (check logs)', 3)
        end
    end)
end)

mp.register_script_message('medios-load-url-event', function(json)
    _lua_log('[LOAD-URL] Event handler called with: ' .. tostring(json or 'nil'))
    local ok, event = pcall(utils.parse_json, json)
    if not ok or type(event) ~= 'table' then
        _lua_log('[LOAD-URL] Failed to parse JSON: ' .. tostring(json))
        mp.osd_message('Failed to parse URL', 2)
        _lua_log('[LOAD-URL] Closing menu due to parse error')
        M._close_uosc_menu_and_sync(LOAD_URL_MENU_TYPE, 'load-url-parse-error')
        return
    end
    _lua_log('[LOAD-URL] Parsed event: type=' .. tostring(event.type) .. ', query=' .. tostring(event.query))
    if event.type ~= 'search' then
        _lua_log('[LOAD-URL] Event type is not search: ' .. tostring(event.type))
        _lua_log('[LOAD-URL] Closing menu due to type mismatch')
        M._close_uosc_menu_and_sync(LOAD_URL_MENU_TYPE, 'load-url-close')
        return
    end

    local url = trim(tostring(event.query or ''))
    _lua_log('[LOAD-URL] Trimmed URL: "' .. url .. '"')
    if url == '' then
        _lua_log('[LOAD-URL] URL is empty')
        _log_all('ERROR', 'Load URL failed: URL is empty')
        mp.osd_message('URL is empty', 2)
        _lua_log('[LOAD-URL] Closing menu due to empty URL')
        M._close_uosc_menu_and_sync(LOAD_URL_MENU_TYPE, 'load-url-empty')
        return
    end

    mp.osd_message('Loading URL...', 1)
    _log_all('INFO', 'Load URL started: ' .. url)
    _lua_log('[LOAD-URL] Starting to load: ' .. url)
    _set_current_web_url(url)
    if _is_ytdlp_url(url) then
        M._prime_web_subtitle_global_defaults('load-url')
        M._apply_web_subtitle_load_defaults('load-url', url)
    end
    _pending_format_change = nil
    pcall(mp.set_property, 'options/ytdl-format', '')
    pcall(mp.set_property, 'file-local-options/ytdl-format', '')
    pcall(mp.set_property, 'ytdl-format', '')
    _lua_log('load-url: cleared stale ytdl format reason=load-url')

    local function close_menu()
        _lua_log('[LOAD-URL] Closing menu and resetting input state')
        if not M._reset_uosc_input_state('load-url-submit') then
            _lua_log('[LOAD-URL] UOSC not loaded, cannot close menu')
        end
        M._schedule_uosc_cursor_resync('load-url-submit')
    end

    -- Close the URL prompt immediately once the user submits. Playback may still
    -- take time to resolve, but the modal should not stay stuck on screen.
    close_menu()

    -- Direct loadfile is only for real media URLs. ytdlp sites (YouTube, etc.)
    -- must go through the helper or ffmpeg hits 403.
    local can_direct = _url_can_direct_load(url) and not _is_ytdlp_url(url)
    local prefer_direct = can_direct
    _lua_log('[LOAD-URL] Checking if URL can be loaded directly: ' .. tostring(can_direct))
    _lua_log('[LOAD-URL] Prefer direct load: ' .. tostring(prefer_direct))

    local direct_ok, direct_loaded = _try_direct_loadfile(url, prefer_direct)
    if direct_ok and direct_loaded then
        _lua_log('[LOAD-URL] Direct loadfile command sent successfully (forced)')
        _log_all('INFO', 'Load URL succeeded via direct load')
        mp.osd_message('URL loaded', 2)
        return
    end
    if direct_ok then
        _lua_log('[LOAD-URL] Direct loadfile command did not load the URL; falling back to helper')
    else
        _lua_log('[LOAD-URL] Direct loadfile command failed; falling back to helper')
    end

    -- Complex streams (YouTube, DASH, etc.) need the pipeline helper.
    _lua_log('[LOAD-URL] URL requires pipeline helper for processing')
    ensure_mpv_ipc_server()
    local helper_ready = ensure_pipeline_helper_running()
    _lua_log('[LOAD-URL] Pipeline helper ready: ' .. tostring(helper_ready))
    
    local function start_pipeline_load()
        -- Use pipeline to download/prepare the URL
        local pipeline_cmd = '.mpv -url ' .. quote_pipeline_arg(url) .. ' -play'
        _lua_log('[LOAD-URL] Sending to pipeline: ' .. pipeline_cmd)
        _lua_log('[LOAD-URL] Pipeline helper ready: ' .. tostring(_is_pipeline_helper_ready()))

        local timeout_timer = nil
        timeout_timer = mp.add_timeout(5, function()
            if timeout_timer then
                mp.osd_message('Still loading... (helper may be resolving URL)', 2)
                _log_all('WARN', 'Load URL still processing after 5 seconds')
                _lua_log('[LOAD-URL] Timeout message shown (helper still processing)')
            end
        end)

        M.run_pipeline(pipeline_cmd, nil, function(resp, err)
            if timeout_timer then
                timeout_timer:kill()
                timeout_timer = nil
            end

            _lua_log('[LOAD-URL] Pipeline callback received: resp=' .. tostring(resp) .. ', err=' .. tostring(err))
            if err then
                _lua_log('[LOAD-URL] Pipeline error: ' .. tostring(err))
                _log_all('ERROR', 'Load URL pipeline failed: ' .. tostring(err))
                mp.osd_message('Load URL failed: ' .. tostring(err), 3)
                return
            end
            _lua_log('[LOAD-URL] URL loaded successfully')
            _log_all('INFO', 'Load URL succeeded')
            mp.osd_message('URL loaded', 2)

            if _is_ytdlp_url(url) then
                _lua_log('[LOAD-URL] URL is yt-dlp compatible, prefetching formats in background')
                mp.add_timeout(0.5, function()
                    M._prefetch_formats_for_url(url)
                    M._schedule_uosc_cursor_resync('file-loaded-web')
                end)
            end
        end)
    end

    if not helper_ready then
        _lua_log('[LOAD-URL] Pipeline helper not available, attempting to start...')
        _log_all('WARN', 'Pipeline helper not running, attempting auto-start')
        mp.osd_message('Starting pipeline helper...', 2)
        
        -- Attempt to start the helper asynchronously
        attempt_start_pipeline_helper_async(function(success)
            if success then
                _lua_log('[LOAD-URL] Helper started successfully, continuing load')
                _log_all('INFO', 'Pipeline helper started successfully')
                start_pipeline_load()
            else
                _lua_log('[LOAD-URL] Failed to start helper')
                _log_all('ERROR', 'Failed to start pipeline helper')
                mp.osd_message('Could not start pipeline helper', 3)
            end
        end)
        return
    end

    start_pipeline_load()
end)

-- Menu integration with UOSC
function M.show_menu()
    _lua_log('[MENU] M.show_menu called')
    M._reset_uosc_input_state('main-menu')
    
    local target = _current_target()
    local selected_store = trim(tostring(_get_selected_store() or ''))
    if not M._store_name_is_visible_in_mpv(selected_store) then
        selected_store = ''
    end
    local download_hint = nil
    if selected_store ~= '' and target and not _extract_store_hash(tostring(target)) then
        download_hint = _store_status_hint_for_url(selected_store, tostring(target), 'save to ' .. selected_store)
    end
    _lua_log('[MENU] current target: ' .. tostring(target))

    -- Build menu items
    -- Note: UOSC expects command strings, not arrays
    local items = {
        { title = "Load URL", value = "script-message medios-load-url" },
        { title = "Get Metadata", value = "script-binding medios-info", hint = "Ctrl+i" },
        { title = "Delete File", value = "script-binding medios-delete", hint = "Ctrl+Del" },
        { title = "Cmd", value = "script-message medios-open-cmd", hint = "screenshot/trim/etc" },
        { title = "Download", value = "script-message medios-download-current", hint = download_hint },
    }

    if _is_ytdlp_url(target) then
        table.insert(items, { title = "Change Format", value = "script-message medios-change-format-current" })
    end

    _lua_log('[MENU] Built ' .. #items .. ' menu items')

    -- Check UOSC availability
    local uosc_ready = ensure_uosc_loaded()
    _lua_log('[MENU] ensure_uosc_loaded returned: ' .. tostring(uosc_ready))
    
    if not uosc_ready then
        _lua_log('[MENU] ERROR: uosc not available; menu cannot open')
        mp.osd_message('Menu unavailable (uosc not loaded)', 3)
        return
    end

    -- Format menu for UOSC
    local menu_data = {
        title = "Medios Macina",
        items = items,
    }

    local json = utils.format_json(menu_data)
    _lua_log('[MENU] Sending menu JSON to uosc: ' .. string.sub(json, 1, 200) .. '...')
    if M._open_uosc_menu(menu_data, 'main-menu') then
        _lua_log('[MENU] Menu command sent successfully')
    else
        _lua_log('[MENU] Failed to send menu command')
        mp.osd_message('Menu error', 3)
    end
end

-- Keybindings with logging wrappers
mp.add_key_binding("m", "medios-menu", function()
    _lua_log('[KEY] m pressed')
    M.show_menu()
end)

mp.add_key_binding("z", "medios-menu-alt", function()
    _lua_log('[KEY] z pressed (alternative menu trigger)')
    M.show_menu()
end)

-- NOTE: mbtn_right is claimed by UOSC globally, so we can't override it here.
-- Instead, use script-message handler below for alternative routing.
mp.add_key_binding("ctrl+i", "medios-info", M.get_file_info)
mp.add_key_binding("ctrl+del", "medios-delete", M.delete_current_file)

-- Lyrics toggle (requested: 'L')
mp.add_key_binding("l", "medeia-lyric-toggle", lyric_toggle)
mp.add_key_binding("L", "medeia-lyric-toggle-shift", lyric_toggle)

-- Script message handler for input.conf routing (right-click via input.conf)
mp.register_script_message('medios-show-menu', function()
    _lua_log('[input.conf] medios-show-menu called')
    M.show_menu()
end)

-- Start the persistent pipeline helper eagerly at launch.
-- This avoids spawning Python per command and works cross-platform via MPV IPC.
mp.add_timeout(0, function()
    pcall(ensure_mpv_ipc_server)
    pcall(_lua_log, 'medeia-lua loaded version=' .. MEDEIA_LUA_VERSION)
    local ok_subtitle_defaults, subtitle_defaults_err = pcall(M._prime_web_subtitle_global_defaults, 'startup')
    if not ok_subtitle_defaults then
        _lua_log('web-subtitles: startup defaults failed err=' .. tostring(subtitle_defaults_err))
    end

    local ok_helper, helper_err = pcall(function()
        attempt_start_pipeline_helper_async(function(success)
            if success then
                _lua_log('helper-auto-start succeeded')
            else
                _lua_log('helper-auto-start failed')
            end
        end)
    end)
    if not ok_helper then
        _lua_log('helper-auto-start raised: ' .. tostring(helper_err))
    end

    local ok_lyric, lyric_err = pcall(function()
        M._attempt_start_lyric_helper_async('startup')
    end)
    if not ok_lyric then
        _lua_log('lyric-helper auto-start raised: ' .. tostring(lyric_err))
    end
    
    -- Force-claim mbtn_right so the Medios menu fires reliably regardless of
    -- whether uosc has a cursor zone active at the click position.
    -- mp.add_forced_key_binding has higher priority than uosc's force-group.
    -- Use complex=true to get the event type and fire only on button release.
    local ok_rclick, rclick_err = pcall(function()
        mp.add_forced_key_binding('mbtn_right', 'medios-mbtn-right-forced', function(e)
            if e and e.event == 'up' then
                _lua_log('[KEY] mbtn_right up -> show_menu')
                M.show_menu()
            end
        end, {complex = true, repeatable = false})
    end)
    if ok_rclick then
        _lua_log('[KEY] registered forced mbtn_right binding')
    else
        _lua_log('[KEY] forced mbtn_right failed: ' .. tostring(rclick_err) .. ' (falling back to input.conf)')
    end

    -- Some mpv/uosc input paths emit double-right-click as `mbtn_right_dbl`.
    -- Register it explicitly so menu invocation still works and mpv doesn't
    -- warn about missing binding for this key.
    local ok_rclick_dbl, rclick_dbl_err = pcall(function()
        mp.add_forced_key_binding('mbtn_right_dbl', 'medios-mbtn-right-dbl-forced', function()
            _lua_log('[KEY] mbtn_right_dbl -> show_menu')
            M.show_menu()
        end, {repeatable = false})
    end)
    if ok_rclick_dbl then
        _lua_log('[KEY] registered forced mbtn_right_dbl binding')
    else
        _lua_log('[KEY] forced mbtn_right_dbl failed: ' .. tostring(rclick_dbl_err) .. ' (falling back to input.conf)')
    end
end)

return M
