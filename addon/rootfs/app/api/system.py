"""
System API - System status and configuration
"""

import os
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from typing import Dict, Any
import json
import yaml
import aiofiles
import zipfile
import io
from datetime import datetime
from lxml import etree
from pydantic import BaseModel

router = APIRouter()

# Version should match config.yaml
VERSION = "1.3.0"

# Path to HA addon options file (written by the Supervisor from config.yaml options)
OPTIONS_FILE = "/data/options.json"


# ─────────────────────────────────────────────
# MQTT config helpers
# ─────────────────────────────────────────────

def _read_options() -> dict:
    """Read /data/options.json written by HA Supervisor."""
    if os.path.exists(OPTIONS_FILE):
        with open(OPTIONS_FILE, "r") as f:
            return json.load(f)
    return {}


def _safe_int(value, default: int) -> int:
    """Convert value to int safely, returning default on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _write_options(data: dict) -> None:
    """Persist options back to /data/options.json."""
    with open(OPTIONS_FILE, "w") as f:
        json.dump(data, f, indent=2)


class MQTTConfig(BaseModel):
    mqtt_host: str
    mqtt_port: int
    mqtt_user: str
    mqtt_pwd: str
    discovery_prefix: str = "homeassistant"
    prefix: str = "enoceanmqtt"
    client_id: str = "enocean_gateway"


# ─────────────────────────────────────────────
# MQTT config endpoints
# ─────────────────────────────────────────────

@router.get("/mqtt-config")
async def get_mqtt_config() -> Dict[str, Any]:
    """Return the current MQTT broker configuration.

    Reads from /data/options.json (HA Supervisor) with fallback to env vars
    so the UI always shows the live values.
    """
    opts = _read_options()
    mqtt_opts = opts.get("mqtt", {})

    return {
        "mqtt_host":        mqtt_opts.get("mqtt_host",        os.getenv("MQTT_HOST", "")),
        "mqtt_port":        _safe_int(mqtt_opts.get("mqtt_port", os.getenv("MQTT_PORT", "")), 1883),
        "mqtt_user":        mqtt_opts.get("mqtt_user",        os.getenv("MQTT_USER", "")),
        # Never expose the password value – just whether it is set
        "mqtt_pwd_set":     bool(mqtt_opts.get("mqtt_pwd",    os.getenv("MQTT_PASSWORD", ""))),
        "discovery_prefix": mqtt_opts.get("discovery_prefix", os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant")),
        "prefix":           mqtt_opts.get("prefix",           os.getenv("MQTT_PREFIX", "enoceanmqtt")),
        "client_id":        mqtt_opts.get("client_id",        os.getenv("MQTT_CLIENT_ID", "enocean_gateway")),
    }


@router.post("/mqtt-config")
async def save_mqtt_config(config: MQTTConfig, request: Request) -> Dict[str, Any]:
    """Save MQTT broker configuration and reconnect.

    Writes the new values into /data/options.json under the ``mqtt`` key,
    then reconnects the live MQTTHandler so changes take effect immediately
    without restarting the addon.
    """
    # 1. Persist to options file
    opts = _read_options()
    opts["mqtt"] = {
        "mqtt_host":        config.mqtt_host,
        "mqtt_port":        config.mqtt_port,
        "mqtt_user":        config.mqtt_user,
        # Keep existing password if the caller sent an empty string
        # (the UI never sends back the masked value)
        "mqtt_pwd":         config.mqtt_pwd if config.mqtt_pwd else opts.get("mqtt", {}).get("mqtt_pwd", ""),
        "discovery_prefix": config.discovery_prefix,
        "prefix":           config.prefix,
        "client_id":        config.client_id,
    }
    _write_options(opts)

    # 2. Reconnect live MQTT handler with new credentials
    mqtt_handler = request.app.state.mqtt_handler
    if mqtt_handler:
        try:
            if mqtt_handler.is_connected:
                await mqtt_handler.disconnect()

            mqtt_handler.host     = config.mqtt_host
            mqtt_handler.port     = config.mqtt_port
            mqtt_handler.username = config.mqtt_user
            mqtt_handler.password = opts["mqtt"]["mqtt_pwd"]
            mqtt_handler.prefix   = config.prefix

            await mqtt_handler.connect()
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Config saved but reconnect failed: {e}"
            )

    return {"status": "saved", "reconnected": mqtt_handler is not None}


# ─────────────────────────────────────────────
# Existing endpoints (unchanged)
# ─────────────────────────────────────────────

@router.get("/status")
async def get_status(request: Request) -> Dict[str, Any]:
    """Get system status"""
    mqtt_handler = request.app.state.mqtt_handler
    serial_handler = request.app.state.serial_handler
    device_manager = request.app.state.device_manager
    eep_manager = request.app.state.eep_manager

    return {
        "mqtt": {
            "connected": mqtt_handler.is_connected if mqtt_handler else False,
            "host": os.getenv("MQTT_HOST", "not configured"),
            "prefix": os.getenv("MQTT_PREFIX", "enocean")
        },
        "enocean": {
            "connected": serial_handler.is_connected if serial_handler else False,
            "port": os.getenv("ENOCEAN_PORT", "not configured")
        },
        "devices": {
            "count": device_manager.device_count if device_manager else 0
        },
        "profiles": {
            "count": eep_manager.profile_count if eep_manager else 0
        },
        "version": VERSION
    }


@router.get("/config")
async def get_config(request: Request) -> Dict[str, Any]:
    """Get current configuration"""
    return {
        "mqtt": {
            "host": os.getenv("MQTT_HOST", ""),
            "port": os.getenv("MQTT_PORT", "1883"),
            "prefix": os.getenv("MQTT_PREFIX", "enocean"),
            "discovery_prefix": os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant"),
            "client_id": os.getenv("MQTT_CLIENT_ID", "enocean_gateway")
        },
        "enocean": {
            "port": os.getenv("ENOCEAN_PORT", "")
        },
        "logging": {
            "level": os.getenv("LOG_LEVEL", "info")
        },
        "paths": {
            "config": request.app.state.config_path
        }
    }


@router.get("/logs")
async def get_logs(lines: int = 100, request: Request = None) -> Dict[str, Any]:
    """Get recent log entries"""
    return {
        "logs": [],
        "message": "Log streaming not yet implemented"
    }


@router.post("/export")
async def export_all(request: Request):
    """Export all configuration as ZIP file"""
    config_path = request.app.state.config_path

    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        devices_file = os.path.join(config_path, "devices.yaml")
        if os.path.exists(devices_file):
            zf.write(devices_file, "devices.yaml")

        legacy_devices = os.path.join(config_path, "enoceanmqtt.devices")
        if os.path.exists(legacy_devices):
            zf.write(legacy_devices, "enoceanmqtt.devices")

        mappings_file = os.path.join(config_path, "mapping.yaml")
        if os.path.exists(mappings_file):
            zf.write(mappings_file, "mapping.yaml")

        custom_eep_path = os.path.join(config_path, "custom_eep")
        if os.path.exists(custom_eep_path):
            for filename in os.listdir(custom_eep_path):
                if filename.endswith(".yaml"):
                    filepath = os.path.join(custom_eep_path, filename)
                    zf.write(filepath, f"custom_eep/{filename}")

        user_eep = os.path.join(config_path, "EEP.xml")
        if os.path.exists(user_eep):
            zf.write(user_eep, "EEP.xml")

        overrides_file = os.path.join(config_path, "mapping_overrides.yaml")
        if os.path.exists(overrides_file):
            zf.write(overrides_file, "mapping_overrides.yaml")

        metadata = {
            "exported_at": datetime.now().isoformat(),
            "version": VERSION,
            "device_manager": request.app.state.device_manager.device_count if request.app.state.device_manager else 0,
            "eep_manager": request.app.state.eep_manager.profile_count if request.app.state.eep_manager else 0
        }
        zf.writestr("export_info.yaml", yaml.dump(metadata, default_flow_style=False, allow_unicode=True))

    zip_buffer.seek(0)

    filename = f"enocean_config_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.post("/import")
async def import_all(file: UploadFile = File(...), request: Request = None) -> Dict[str, Any]:
    """Import configuration from ZIP file"""
    if not request:
        raise HTTPException(status_code=500, detail="Request context required")

    config_path = request.app.state.config_path
    device_manager = request.app.state.device_manager

    try:
        content = await file.read()
        zip_buffer = io.BytesIO(content)

        imported = {
            "devices": False,
            "mappings": False,
            "mapping_overrides": False,
            "custom_profiles": 0,
            "eep_xml": False
        }

        with zipfile.ZipFile(zip_buffer, 'r') as zf:
            for filename in zf.namelist():
                if filename in ("devices.json", "devices.yaml"):
                    raw = zf.read(filename)
                    if filename.endswith(".json"):
                        devices_data = json.loads(raw)
                    else:
                        devices_data = yaml.safe_load(raw) or {}
                    devices_file = os.path.join(config_path, "devices.yaml")
                    os.makedirs(config_path, exist_ok=True)
                    async with aiofiles.open(devices_file, 'w') as f:
                        await f.write(yaml.dump(devices_data, default_flow_style=False, allow_unicode=True))
                    imported["devices"] = True

                    if device_manager:
                        await device_manager.load_devices()

                elif filename == "mapping.yaml":
                    mappings_data = zf.read(filename)
                    mappings_file = os.path.join(config_path, "mapping.yaml")
                    os.makedirs(config_path, exist_ok=True)
                    async with aiofiles.open(mappings_file, 'wb') as f:
                        await f.write(mappings_data)
                    imported["mappings"] = True

                elif filename.startswith("custom_eep/") and filename.endswith(".yaml"):
                    profile_data = zf.read(filename)
                    profile_name = os.path.basename(filename)
                    custom_path = os.path.join(config_path, "custom_eep")
                    os.makedirs(custom_path, exist_ok=True)
                    async with aiofiles.open(os.path.join(custom_path, profile_name), 'wb') as f:
                        await f.write(profile_data)
                    imported["custom_profiles"] += 1

                elif filename == "EEP.xml":
                    eep_data = zf.read(filename)
                    eep_path = os.path.join(config_path, "EEP.xml")
                    os.makedirs(config_path, exist_ok=True)
                    async with aiofiles.open(eep_path, 'wb') as f:
                        await f.write(eep_data)
                    imported["eep_xml"] = True

                elif filename in ("mapping_overrides.json", "mapping_overrides.yaml"):
                    raw = zf.read(filename)
                    if filename.endswith(".json"):
                        overrides_data = json.loads(raw)
                    else:
                        overrides_data = yaml.safe_load(raw) or {}
                    overrides_path = os.path.join(config_path, "mapping_overrides.yaml")
                    os.makedirs(config_path, exist_ok=True)
                    async with aiofiles.open(overrides_path, 'w') as f:
                        await f.write(yaml.dump(overrides_data, default_flow_style=False, allow_unicode=True))
                    imported["mapping_overrides"] = True

        eep_manager = request.app.state.eep_manager
        if eep_manager and (imported.get("eep_xml") or imported.get("custom_profiles", 0) > 0):
            eep_manager.profiles.clear()
            await eep_manager.initialize()

        if imported.get("mapping_overrides") and eep_manager:
            await eep_manager._load_overrides()

        return {"status": "imported", "details": imported}

    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid ZIP file")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Import failed: {e}")


@router.get("/eep-info")
async def get_eep_info(request: Request) -> Dict[str, Any]:
    """Get EEP.xml status information"""
    eep_manager = request.app.state.eep_manager
    if not eep_manager:
        raise HTTPException(status_code=500, detail="EEP Manager not initialized")
    return eep_manager.get_eep_info()


@router.post("/upload-eep")
async def upload_eep(file: UploadFile = File(...), request: Request = None) -> Dict[str, Any]:
    """Upload custom EEP.xml file"""
    if not request:
        raise HTTPException(status_code=500, detail="Request context required")

    config_path = request.app.state.config_path
    eep_manager = request.app.state.eep_manager

    try:
        content = await file.read()

        try:
            root = etree.fromstring(content)
            telegrams = root.findall(".//telegram")
            if not telegrams:
                raise HTTPException(status_code=400, detail="Invalid EEP.xml: no telegram elements found")
        except etree.XMLSyntaxError as e:
            raise HTTPException(status_code=400, detail=f"Invalid XML: {e}")

        eep_path = os.path.join(config_path, "EEP.xml")
        os.makedirs(config_path, exist_ok=True)
        async with aiofiles.open(eep_path, 'wb') as f:
            await f.write(content)

        if eep_manager:
            eep_manager.profiles.clear()
            await eep_manager.initialize()

        return {
            "status": "uploaded",
            "info": eep_manager.get_eep_info() if eep_manager else {}
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")


@router.delete("/delete-eep")
async def delete_eep(request: Request) -> Dict[str, Any]:
    """Delete custom EEP.xml and revert to bundled"""
    config_path = request.app.state.config_path
    eep_manager = request.app.state.eep_manager

    eep_path = os.path.join(config_path, "EEP.xml")

    if not os.path.exists(eep_path):
        raise HTTPException(status_code=404, detail="No custom EEP.xml found")

    try:
        os.remove(eep_path)

        if eep_manager:
            eep_manager.profiles.clear()
            await eep_manager.initialize()

        return {
            "status": "deleted",
            "info": eep_manager.get_eep_info() if eep_manager else {}
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")


BACKUP_DIR = "backups"


def _get_backup_dir(config_path: str) -> str:
    backup_dir = os.path.join(config_path, BACKUP_DIR)
    os.makedirs(backup_dir, exist_ok=True)
    return backup_dir


@router.get("/backups")
async def list_backups(request: Request):
    """List all local backups"""
    config_path = request.app.state.config_path
    backup_dir = _get_backup_dir(config_path)
    backups = []

    for filename in sorted(os.listdir(backup_dir), reverse=True):
        if not filename.endswith(".zip"):
            continue
        filepath = os.path.join(backup_dir, filename)
        stat = os.stat(filepath)

        devices = 0
        version = "?"
        try:
            with zipfile.ZipFile(filepath, 'r') as zf:
                if "export_info.yaml" in zf.namelist():
                    meta = yaml.safe_load(zf.read("export_info.yaml"))
                    devices = meta.get("device_manager", 0)
                    version = meta.get("version", "?")
                elif "export_info.json" in zf.namelist():
                    meta = json.loads(zf.read("export_info.json"))
                    devices = meta.get("device_manager", 0)
                    version = meta.get("version", "?")
        except Exception:
            pass

        backups.append({
            "filename": filename,
            "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "size": stat.st_size,
            "devices": devices,
            "version": version,
        })

    return backups


@router.post("/backup")
async def create_backup(request: Request) -> Dict[str, Any]:
    """Create a local backup ZIP"""
    config_path = request.app.state.config_path
    backup_dir = _get_backup_dir(config_path)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{timestamp}.zip"
    filepath = os.path.join(backup_dir, filename)

    device_manager = request.app.state.device_manager
    eep_manager = request.app.state.eep_manager

    with zipfile.ZipFile(filepath, 'w', zipfile.ZIP_DEFLATED) as zf:
        devices_file = os.path.join(config_path, "devices.yaml")
        if os.path.exists(devices_file):
            zf.write(devices_file, "devices.yaml")

        legacy_devices = os.path.join(config_path, "enoceanmqtt.devices")
        if os.path.exists(legacy_devices):
            zf.write(legacy_devices, "enoceanmqtt.devices")

        mappings_file = os.path.join(config_path, "mapping.yaml")
        if os.path.exists(mappings_file):
            zf.write(mappings_file, "mapping.yaml")

        custom_eep_path = os.path.join(config_path, "custom_eep")
        if os.path.exists(custom_eep_path):
            for fname in os.listdir(custom_eep_path):
                if fname.endswith(".yaml"):
                    zf.write(os.path.join(custom_eep_path, fname), f"custom_eep/{fname}")

        user_eep = os.path.join(config_path, "EEP.xml")
        if os.path.exists(user_eep):
            zf.write(user_eep, "EEP.xml")

        overrides_file = os.path.join(config_path, "mapping_overrides.yaml")
        if os.path.exists(overrides_file):
            zf.write(overrides_file, "mapping_overrides.yaml")

        metadata = {
            "exported_at": datetime.now().isoformat(),
            "version": VERSION,
            "device_manager": device_manager.device_count if device_manager else 0,
            "eep_manager": eep_manager.profile_count if eep_manager else 0
        }
        zf.writestr("export_info.yaml", yaml.dump(metadata, default_flow_style=False, allow_unicode=True))

    return {"filename": filename, "status": "created"}


@router.get("/backup/download/{filename}")
async def download_backup(filename: str, request: Request):
    """Download a backup file"""
    config_path = request.app.state.config_path
    filepath = os.path.join(_get_backup_dir(config_path), filename)

    if not os.path.exists(filepath) or not filename.endswith(".zip"):
        raise HTTPException(status_code=404, detail="Backup not found")

    return FileResponse(filepath, filename=filename, media_type="application/zip")


@router.post("/backup/restore/{filename}")
async def restore_backup(filename: str, request: Request) -> Dict[str, Any]:
    """Restore from a backup file"""
    config_path = request.app.state.config_path
    filepath = os.path.join(_get_backup_dir(config_path), filename)

    if not os.path.exists(filepath) or not filename.endswith(".zip"):
        raise HTTPException(status_code=404, detail="Backup not found")

    device_manager = request.app.state.device_manager
    eep_manager = request.app.state.eep_manager

    imported = {
        "devices": False,
        "mappings": False,
        "mapping_overrides": False,
        "custom_profiles": 0,
        "eep_xml": False,
    }

    try:
        with zipfile.ZipFile(filepath, 'r') as zf:
            for name in zf.namelist():
                if name in ("devices.json", "devices.yaml"):
                    raw = zf.read(name)
                    if name.endswith(".json"):
                        devices_data = json.loads(raw)
                    else:
                        devices_data = yaml.safe_load(raw) or {}
                    devices_file = os.path.join(config_path, "devices.yaml")
                    async with aiofiles.open(devices_file, 'w') as f:
                        await f.write(yaml.dump(devices_data, default_flow_style=False, allow_unicode=True))
                    imported["devices"] = True
                    if device_manager:
                        await device_manager.load_devices()

                elif name == "mapping.yaml":
                    data = zf.read(name)
                    async with aiofiles.open(os.path.join(config_path, "mapping.yaml"), 'wb') as f:
                        await f.write(data)
                    imported["mappings"] = True

                elif name.startswith("custom_eep/") and name.endswith(".yaml"):
                    data = zf.read(name)
                    custom_path = os.path.join(config_path, "custom_eep")
                    os.makedirs(custom_path, exist_ok=True)
                    async with aiofiles.open(os.path.join(custom_path, os.path.basename(name)), 'wb') as f:
                        await f.write(data)
                    imported["custom_profiles"] += 1

                elif name == "EEP.xml":
                    data = zf.read(name)
                    async with aiofiles.open(os.path.join(config_path, "EEP.xml"), 'wb') as f:
                        await f.write(data)
                    imported["eep_xml"] = True

                elif name in ("mapping_overrides.json", "mapping_overrides.yaml"):
                    raw = zf.read(name)
                    if name.endswith(".json"):
                        overrides_data = json.loads(raw)
                    else:
                        overrides_data = yaml.safe_load(raw) or {}
                    overrides_path = os.path.join(config_path, "mapping_overrides.yaml")
                    async with aiofiles.open(overrides_path, 'w') as f:
                        await f.write(yaml.dump(overrides_data, default_flow_style=False, allow_unicode=True))
                    imported["mapping_overrides"] = True

        if eep_manager and (imported.get("eep_xml") or imported.get("custom_profiles", 0) > 0):
            eep_manager.profiles.clear()
            await eep_manager.initialize()

        if imported.get("mapping_overrides") and eep_manager:
            await eep_manager._load_overrides()

        return {"status": "restored", "details": imported}

    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Corrupt backup file")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Restore failed: {e}")


@router.delete("/backup/{filename}")
async def delete_backup(filename: str, request: Request) -> Dict[str, str]:
    """Delete a backup file"""
    config_path = request.app.state.config_path
    filepath = os.path.join(_get_backup_dir(config_path), filename)

    if not os.path.exists(filepath) or not filename.endswith(".zip"):
        raise HTTPException(status_code=404, detail="Backup not found")

    os.remove(filepath)
    return {"status": "deleted"}


@router.post("/restart")
async def restart_services(request: Request) -> Dict[str, str]:
    """Restart EnOcean and MQTT services"""
    mqtt_handler = request.app.state.mqtt_handler
    serial_handler = request.app.state.serial_handler

    try:
        if serial_handler and serial_handler.is_connected:
            await serial_handler.disconnect()

        if mqtt_handler and mqtt_handler.is_connected:
            await mqtt_handler.disconnect()

        if mqtt_handler:
            await mqtt_handler.connect()

        if serial_handler:
            await serial_handler.connect()

        return {"status": "restarted"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Restart failed: {e}")
