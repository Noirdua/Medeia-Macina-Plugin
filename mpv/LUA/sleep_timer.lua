local mp = require 'mp'
local utils = require 'mp.utils'

local SLEEP_MENU_TYPE = 'medeia_sleep_timer_prompt'

local _timer = nil

local function _trim(s)
    s = tostring(s or '')
    s = s:gsub('^%s+', '')
    s = s:gsub('%s+$', '')
    return s
end

local function _cancel_timer()
    if _timer ~= nil then
        pcall(function()
            _timer:kill()
        end)
        _timer = nil
    end
end

local function _parse_minutes(text)
    local s = _trim(text)
    if s == '' then
        return nil
    end

    local lower = s:lower()

    -- allow: 15, 15m, 1h, 1.5h
    local hours = lower:match('^([%d%.]+)%s*h$')
    if hours then
        local v = tonumber(hours)
        if v and v > 0 then
            return v * 60
        end
        return nil
    end

    local mins = lower:match('^([%d%.]+)%s*m$')
    if mins then
        local v = tonumber(mins)
        if v and v > 0 then
            return v
        end
        return nil
    end

    local v = tonumber(lower)
    if v and v > 0 then
        return v
    end

    return nil
end

local function _open_prompt()
    local menu_data = {
        type = SLEEP_MENU_TYPE,
        title = 'Sleep Timer',
        search_style = 'palette',
        search_debounce = 'submit',
        on_search = 'callback',
        footnote = 'Enter minutes (e.g. 30) then press Enter.',
        callback = { mp.get_script_name(), 'medeia-sleep-timer-event' },
        items = {},
    }

    local json = utils.format_json(menu_data)
    local ok = pcall(function()
        mp.commandv('script-message-to', 'uosc', 'open-menu', json)
    end)
    if not ok then
        mp.osd_message('Sleep timer: uosc not available', 2.0)
    end
end

local function _handle_event(json)
    local ok, ev = pcall(utils.parse_json, json)
    if not ok or type(ev) ~= 'table' then
        return
    end
    if ev.type ~= 'search' then
        return
    end

    local minutes = _parse_minutes(ev.query or '')
    if not minutes then
        mp.osd_message('Sleep timer cancelled', 1.0)
        _cancel_timer()
        return
    end

    _cancel_timer()

    local seconds = math.floor(minutes * 60)
    _timer = mp.add_timeout(seconds, function()
        mp.osd_message('Sleep timer: closing mpv', 1.5)
        mp.commandv('quit')
    end)

    mp.osd_message(string.format('Sleep timer set: %d min', math.floor(minutes + 0.5)), 1.5)

    pcall(function()
        mp.commandv('script-message-to', 'uosc', 'close-menu', SLEEP_MENU_TYPE)
    end)
end

mp.register_script_message('medeia-sleep-timer', _open_prompt)
mp.register_script_message('medeia-sleep-timer-event', _handle_event)

return {
    open_prompt = _open_prompt,
}
