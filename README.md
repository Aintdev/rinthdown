# RinthDown

A command-line Modrinth mod and modpack downloader.

## Features

- Download mods for a specific Minecraft version and mod loader.
- Select stable or latest mod versions.
- Automatically download required dependencies.
- Download and extract Modrinth modpacks.
- Support for client and server installations.

## Installation

```bash
pip install rinthdown
```

## Usage

```bash
rinthdown --help
```

Example: Download mods for Minecraft Server 1.21.1 using NeoForge:

```bash
rinthdown -s -mcv 1.21.1 -l neoforge -latest -p sodium iris
```

## License

MIT