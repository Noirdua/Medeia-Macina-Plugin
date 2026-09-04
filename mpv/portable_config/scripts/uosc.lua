---@diagnostic disable: undefined-global
-- uosc loader shim for this portable_config layout
-- Ensures mpv loads uosc as script name "uosc" from portable_config/scripts.

local utils = require('mp.utils')

local function add_package_path(dir)
	-- Allow require('lib/std') etc to resolve inside the uosc directory.
	package.path = utils.join_path(dir, '?.lua') .. ';' .. utils.join_path(dir, '?/init.lua') .. ';' .. package.path
end

local function _this_script_dir()
	local info = debug.getinfo(1, 'S')
	local src = info and info.source or nil
	if type(src) ~= 'string' then
		return ''
	end
	if src:sub(1, 1) == '@' then
		src = src:sub(2)
	end
	local dir = src:match('^(.*)[/\\]')
	return dir or ''
end

local scripts_root = mp.get_script_directory()
if type(scripts_root) ~= 'string' or scripts_root == '' then
	scripts_root = _this_script_dir()
end
if type(scripts_root) ~= 'string' then
	scripts_root = ''
end

local msg = require('mp.msg')
msg.info('[uosc-shim] loading uosc...')

-- Your current folder layout is: scripts/uosc/scripts/uosc/main.lua
local uosc_dir = utils.join_path(scripts_root, 'uosc/scripts/uosc')

if not utils.file_info(utils.join_path(uosc_dir, 'main.lua')) then
    msg.error('[uosc-shim] ERROR: main.lua not found at ' .. tostring(uosc_dir))
end

-- uosc uses mp.get_script_directory() to find its adjacent resources (bin/, lib/, etc).
-- Because this loader lives in scripts/, override it so uosc resolves paths correctly.
local _orig_get_script_directory = mp.get_script_directory
mp.get_script_directory = function()
	return uosc_dir
end

add_package_path(uosc_dir)

local ok, err = pcall(dofile, utils.join_path(uosc_dir, 'main.lua'))
if not ok then
    msg.error('[uosc-shim] ERROR during dofile: ' .. tostring(err))
else
    msg.info('[uosc-shim] uosc loaded successfully')
    -- Notify main.lua that we are alive
    mp.commandv('script-message', 'uosc-version', 'bundled')
end
