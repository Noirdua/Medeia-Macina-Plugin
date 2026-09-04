-- Trim file directly in MPV using FFmpeg
-- This script handles the actual video trimming with FFmpeg subprocess
-- Then passes the trimmed file to Python for upload/metadata handling

local mp = require "mp"
local utils = require "mp.utils"

local trim = {}

-- Configuration for trim presets
trim.config = {
    output_dir = os.getenv('TEMP') or os.getenv('TMP') or '/tmp',  -- use temp dir by default
    container = "auto",
    scale = "640:-2",  -- Scale to 640 width, -2 ensures even height for codec
    osd_duration = 2000,
}

-- Quality presets for video trimming
trim.presets = {
    copy   = { video_codec="copy", audio_codec="copy" },
    high   = { video_codec="libx264", crf="18", preset="slower", audio_codec="aac", audio_bitrate="192k" },
    medium = { video_codec="libx264", crf="20", preset="medium", audio_codec="aac", audio_bitrate="128k" },
    fast   = { video_codec="libx264", crf="23", preset="fast", audio_codec="aac", audio_bitrate="96k" },
    tiny   = { video_codec="libx264", crf="28", preset="ultrafast", audio_codec="aac", audio_bitrate="64k" },
}

trim.current_quality = "medium"

-- Get active preset with current quality
local function _get_active_preset()
    local preset = trim.presets[trim.current_quality] or {}
    local merged = {}
    for k, v in pairs(preset) do
        merged[k] = v
    end
    for k, v in pairs(trim.config) do
        if merged[k] == nil then
            merged[k] = v
        end
    end
    return merged
end

-- Extract title from file path, handling special URL formats
local function _parse_file_title(filepath)
    -- For torrent URLs, try to extract meaningful filename
    if filepath:match("torrentio%.strem%.fun") then
        -- Format: https://torrentio.strem.fun/resolve/alldebrid/.../filename/0/filename
        local filename = filepath:match("([^/]+)/0/[^/]+$")
        if filename then
            filename = filename:gsub("%.mkv$", ""):gsub("%.mp4$", ""):gsub("%.avi$", "")
            return filename
        end
    end
    
    -- Standard file path
    local dir, name = utils.split_path(filepath)
    return name:gsub("%..+$", "")
end

-- Format time duration as "1h3m-1h3m15s"
local function _format_time_range(start_sec, end_sec)
    local function _sec_to_str(sec)
        local h = math.floor(sec / 3600)
        local m = math.floor((sec % 3600) / 60)
        local s = math.floor(sec % 60)
        
        local parts = {}
        if h > 0 then table.insert(parts, h .. "h") end
        if m > 0 then table.insert(parts, m .. "m") end
        if s > 0 or #parts == 0 then table.insert(parts, s .. "s") end
        
        return table.concat(parts)
    end
    
    return _sec_to_str(start_sec) .. "-" .. _sec_to_str(end_sec)
end

-- Trim file using FFmpeg with range in format "1h3m-1h3m15s"
-- Returns: (success, output_path, error_msg)
function trim.trim_file(input_file, range_str, temp_dir)
    if not input_file or input_file == "" then
        return false, nil, "No input file specified"
    end
    
    if not range_str or range_str == "" then
        return false, nil, "No range specified (format: 1h3m-1h3m15s)"
    end
    
    -- Use provided temp_dir or fall back to config
    if not temp_dir or temp_dir == "" then
        temp_dir = trim.config.output_dir
    end
    
    -- Parse range string "1h3m-1h3m15s"
    local start_str, end_str = range_str:match("^([^-]+)-(.+)$")
    if not start_str or not end_str then
        return false, nil, "Invalid range format. Use: 1h3m-1h3m15s"
    end
    
    -- Convert time string to seconds
    local function _parse_time(time_str)
        local sec = 0
        local h = time_str:match("(%d+)h")
        local m = time_str:match("(%d+)m")
        local s = time_str:match("(%d+)s")
        
        if h then sec = sec + tonumber(h) * 3600 end
        if m then sec = sec + tonumber(m) * 60 end
        if s then sec = sec + tonumber(s) end
        
        return sec
    end
    
    local start_time = _parse_time(start_str)
    local end_time = _parse_time(end_str)
    local duration = end_time - start_time
    
    if duration <= 0 then
        return false, nil, "Invalid range: end time must be after start time"
    end
    
    -- Prepare output path
    local dir, name = utils.split_path(input_file)
    
    -- If input is a URL, extract filename from URL path
    if input_file:match("^https?://") then
        -- For URLs, try to extract the meaningful filename
        name = input_file:match("([^/]+)$") or "stream"
        dir = trim.config.output_dir
    end
    
    local out_dir = trim.config.output_dir
    local ext = (trim.config.container == "auto") and input_file:match("^.+(%..+)$") or ("." .. trim.config.container)
    local base_name = name:gsub("%..+$", "")
    local out_path = utils.join_path(out_dir, base_name .. "_" .. range_str .. ext)
    
    -- Normalize path to use consistent backslashes on Windows
    out_path = out_path:gsub("/", "\\")
    
    -- Build FFmpeg command
    local p = _get_active_preset()
    local args = { "ffmpeg", "-y", "-ss", tostring(start_time), "-i", input_file, "-t", tostring(duration) }
    
    -- Video codec
    if p.video_codec == "copy" then
        table.insert(args, "-c:v")
        table.insert(args, "copy")
    else
        table.insert(args, "-c:v")
        table.insert(args, p.video_codec)
        if p.crf and p.crf ~= "" then
            table.insert(args, "-crf")
            table.insert(args, p.crf)
        end
        if p.preset and p.preset ~= "" then
            table.insert(args, "-preset")
            table.insert(args, p.preset)
        end
    end
    
    -- Audio codec
    if p.audio_codec == "copy" then
        table.insert(args, "-c:a")
        table.insert(args, "copy")
    else
        table.insert(args, "-c:a")
        table.insert(args, p.audio_codec)
        if p.audio_bitrate and p.audio_bitrate ~= "" then
            table.insert(args, "-b:a")
            table.insert(args, p.audio_bitrate)
        end
    end
    
    table.insert(args, out_path)
    
    -- Execute FFmpeg synchronously
    local result = mp.command_native({ name = "subprocess", args = args, capture_stdout = true, capture_stderr = true })
    
    if not result or result.status ~= 0 then
        local error_msg = result and result.stderr or "Unknown FFmpeg error"
        return false, nil, "FFmpeg failed: " .. error_msg
    end
    
    return true, out_path, nil
end

-- Cycle to next quality preset
function trim.cycle_quality()
    local presets = { "copy", "high", "medium", "fast", "tiny" }
    local idx = 1
    for i, v in ipairs(presets) do
        if v == trim.current_quality then
            idx = i
            break
        end
    end
    trim.current_quality = presets[(idx % #presets) + 1]
    return trim.current_quality
end

return trim
