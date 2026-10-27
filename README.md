# Newts Server Control Panel (NSCP)
<span style="display: flex; justify-content: center; align-items: center;">
<img alt="backups page" style="border-radius:5%" width="200px" src="gallery/icon-trans.png">
</span>
A standalone Linux-based Minecraft server management panel featuring backups, scheduling, live console, RCON player tracking, plugin manager, file explorer, text editor, and more.



## Features
- Web dashboard for real-time server monitoring
- multi user login system
- Start/stop controls with auto-restart
- Full console streaming + command input
- Backup system with retention management
- Scheduler for automated commands
- Discord webhook integration
- RCON-powered player list + join history
- Player management (op/deop, whitelist)
- automatic owner whitelist and op settings
- File manager with upload/download/delete
- Built-in text editor (.yml, .json, .txt, .md, .properties)
- Plugin/Mod manager (enable/disable `.jar.disabled`)
- server.properties editor
- License acceptance gate (scroll-to-accept)
- Hardware usage (CPU/RAM/Disk) monitoring

## Requirements
- Python 3.9+
- pip
- Linux OS
- Bash
- Minecraft server jar (java/Paper/Spigot/Purpur/etc.)
- systemd
- RCON enabled

## Installation Guide

### 1. Extract NSCP
Place the folder somewhere permanent:
```
/DATA/AppData/servers/server-id/nscp2.1
```

### 2. Configure config.yml
(Edit panel, server_dir, start_command, backups, webhook, etc.)

### 3. Allow Execution of runcp.sh
```
chmod +x runcp.sh
```

### 4. Install Dependencies
#### <red style="color:orange">*only required if you dont use the launch script*</red>
```
pip install -r requirements.txt
```

### 5. Enable RCON
Add to server.properties:
```
enable-rcon=true
rcon.port=25575
rcon.password=your_password
```

### 6. Configure systemd Service
Create `/etc/systemd/system/nscp.service`\
*bellow is a template you can use*
```service
[Unit]
Description=Newts Server Control Panel
After=network.target

[Service]
LimitNOFILE=16384
User=root
WorkingDirectory=/path/to/launch/directory
ExecStart=/path/to/launch/script/runcp.sh
Restart=always
RestartSec=60
Environment="FLASK_ENV=production"

Environment="RCON_HOST=127.0.0.1"
Environment="RCON_PORT=25575"
Environment="RCON_PASS=CHANGE_ME"


[Install]
WantedBy=multi-user.target
```

### 7. First Launch
Visit http://YOUR-IP:8081, accept EULA, login, configure panel.

## Need Help?
Join My Discord: http://discord.newt-tech.com

# Gallery

<details>
  <summary>Login Page</summary>

  ## Login  
  <img alt="login page" src="gallery/login2.1.png">
</details>

<details>
  <summary>Dashboard</summary>

  ## Dashboard  
  <img alt="dashboard page" src="gallery/dashboard2.1.png">
</details>

<details>
  <summary>Console</summary>

  ## Console  
  <img alt="console page" src="gallery/console2.1.png">
</details>

<details>
  <summary>Backups</summary>

  ## Backups  
  <img alt="backups page" src="gallery/backups.png">
</details>

<details>
  <summary>Scheduler</summary>

  ## Scheduler  
  <img alt="scheduler page" src="gallery/scheduler.png">
</details>

<details>
  <summary>Properties</summary>

  ## Properties  
  <img alt="properties page" src="gallery/properties.png">
</details>

<details>
  <summary>Players</summary>

  ## Players  
  <img alt="players page" src="gallery/players.png">
</details>

<details>
  <summary>Bans</summary>

  ## Bans  
  <img alt="bans page" src="gallery/bans.png">
</details>

<details>
  <summary>Plugins</summary>

  ## Plugins  
  <img alt="plugins page" src="gallery/plugins.png">
</details>

<details>
  <summary>File Manager</summary>

  ## File Manager  
  <img alt="file manager page" src="gallery/files.png">
</details>

<details>
  <summary>Settings</summary>

  ## Settings  
  <img alt="settings page" src="gallery/settings2.1.png">
</details>
<details>
  <summary>User Management</summary>

## User Management
  <img alt="settings page" src="gallery/users.png">
</details>
