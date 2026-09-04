# Medeia-Macina-Plugin

Plugin catalog for [Medeia-Macina](https://github.com/Noirdua/Medeia-Macina). The app installs from this git repo with `.plugin`.

## Install in the app

```
.plugin -available
.plugin -add alldebrid
.plugin -update
@1 | .plugin -install
```

Source URL is `plugin_source` in `.config` (default this repository). Override with `MM_PLUGIN_SOURCE`.

## Layout

Each plugin is a folder with `__init__.py`:

```
alldebrid/
  __init__.py          # Plugin subclass
  commands.py          # extra commands (optional)
  api/                 # plugin-owned HTTP
hello/
  __init__.py
```

Required on the Plugin class:

- `PLUGIN_NAME` (folder name)
- `PLUGIN_VERSION`, `PLUGIN_AUTHOR`, `PLUGIN_DESCRIPTION`
- `SUPPORTED_CMDLETS`
- `validate()`, `config_schema()` for credentials
- `CONFIG_HELP` for `.config` instructions

Extra commands must be a `Cmdlet` in `commands.py` with `summary`, `usage`, `examples`, and `detail` so `.help <command>` works. Example: `unlock-link` on AllDebrid.

Authoring details: the app repo `docs/plugin_guide.md`. Validate with `python -m PluginCore.validate`.
