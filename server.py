#!/usr/bin/env python3
"""
Kika Obsidian MCP Server
Enables LLMs to interact with Obsidian vaults directly on disk (no Obsidian app required).
"""

import os
import logging
from typing import Any, Optional
from mcp.server.mcpserver import MCPServer
from obsidian_client import ObsidianVaultClient
from bases import build_base

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- call counter: one JSONL line per tool call (ts, tool, client) ---------
import json as _json, os as _os
from datetime import datetime as _dt, timezone as _tz
from pathlib import Path as _Path

_CALL_LOG = _Path(
    _os.environ.get("KIKA_OBSIDIAN_CALL_LOG")
    or _Path(_os.environ.get("XDG_STATE_HOME") or _Path.home() / ".local" / "state")
    / "kika-obsidian-mcp" / "calls.jsonl"
)

async def _count_tool_calls(_ctx, _call_next):
    """mcp 2.x server middleware; counting must never break the server."""
    if _ctx.method == "tools/call":
        try:
            try:
                _cp = _ctx.session.client_params
                _info = getattr(_cp, "client_info", None) or getattr(_cp, "clientInfo")
                _client, _ver = _info.name, _info.version
            except Exception:
                _client, _ver = "unknown", None
            _CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _CALL_LOG.open("a", encoding="utf-8") as _f:
                _f.write(_json.dumps({
                    "ts": _dt.now(_tz.utc).isoformat(timespec="seconds"),
                    "tool": (_ctx.params or {}).get("name", "?"),
                    "client": _client,
                    "client_version": _ver,
                }) + "\n")
        except Exception:
            pass
    return await _call_next(_ctx)

# Initialize the MCP server (mcp SDK 2.x)
mcp = MCPServer("kika-obsidian", middleware=[_count_tool_calls])
# ---------------------------------------------------------------------------


# Global vault client (initialized when needed)
_vault_client = None
_vault_path = None


def is_read_only() -> bool:
    """Return true when mutating tools should refuse writes."""
    value = os.getenv("OBSIDIAN_READ_ONLY", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def backup_on_write_enabled() -> bool:
    """Return true when existing notes should be backed up before writes."""
    value = os.getenv("OBSIDIAN_BACKUP_ON_WRITE", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def read_only_error() -> dict:
    """Shared response for disabled write operations."""
    return {
        "success": False,
        "error": "This server is running in read-only mode. Set OBSIDIAN_READ_ONLY=false to allow writes.",
    }


def get_vault_client() -> ObsidianVaultClient:
    """Get or create the vault client."""
    global _vault_client, _vault_path
    
    if _vault_client is None:
        if _vault_path is None:
            # Try to get from environment variable
            _vault_path = os.getenv("OBSIDIAN_VAULT_PATH")
            if not _vault_path:
                raise ValueError("Obsidian vault path not configured. Set OBSIDIAN_VAULT_PATH environment variable.")
        
        _vault_client = ObsidianVaultClient(_vault_path, backup_on_write=backup_on_write_enabled())
        logger.info(f"Connected to vault: {_vault_path}")
    
    return _vault_client


@mcp.tool()
def configure_vault(vault_path: str) -> dict:
    """Configure the Obsidian vault path.
    
    Args:
        vault_path: Absolute path to your Obsidian vault
    """
    global _vault_client, _vault_path

    if os.getenv("OBSIDIAN_VAULT_PATH"):
        return {
            "success": False,
            "error": "Vault path is locked by the OBSIDIAN_VAULT_PATH environment variable. Change it in the MCP server config instead.",
        }

    _vault_path = vault_path
    _vault_client = None  # Reset client
    
    try:
        client = get_vault_client()
        return {
            "success": True,
            "message": f"Vault configured successfully: {vault_path}",
            "note_count": len(client.list_notes())
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
def get_note(path: str) -> dict:
    """Get a note by its path.
    
    Args:
        path: Path to the note relative to vault root (e.g., "notes/my-note.md")
    """
    try:
        client = get_vault_client()
        note = client.get_note(path)
        
        if note is None:
            return {"error": f"Note not found: {path}"}
        
        return note
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def create_note(path: str, content: str, title: Optional[str] = None, tags: Optional[list] = None) -> dict:
    """Create a new note.
    
    Args:
        path: Path for the new note (e.g., "notes/new-note.md")
        content: Note content in Markdown
        title: Optional title (defaults to filename)
        tags: Optional list of tags
    """
    try:
        if is_read_only():
            return read_only_error()

        client = get_vault_client()
        
        metadata = {}
        if title:
            metadata['title'] = title
        if tags:
            metadata['tags'] = tags
        
        note = client.create_note(path, content, metadata)
        return note
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def update_note(path: str, content: Optional[str] = None, metadata: Optional[dict] = None) -> dict:
    """Update an existing note.
    
    Args:
        path: Path to the note
        content: New content (optional)
        metadata: Metadata updates (optional)
    """
    try:
        if is_read_only():
            return read_only_error()

        client = get_vault_client()
        note = client.update_note(path, content, metadata)
        return note
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def delete_note(path: str) -> dict:
    """Delete a note permanently.
    
    Args:
        path: Path to the note to delete
    """
    try:
        if is_read_only():
            return read_only_error()

        client = get_vault_client()
        success = client.delete_note(path)
        
        if success:
            return {"success": True, "message": f"Note deleted: {path}"}
        else:
            return {"error": f"Note not found: {path}"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_notes(folder: Optional[str] = None) -> list:
    """List all notes in the vault or a specific folder.
    
    Args:
        folder: Optional folder path (e.g., "notes/" or "projects/")
    """
    try:
        client = get_vault_client()
        notes = client.list_notes(folder)
        
        # Return simplified version for list
        return [
            {
                "path": note['path'],
                "title": note['title'],
                "tags": note.get('tags', [])
            }
            for note in notes
        ]
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
def search_notes(query: str, folder: Optional[str] = None) -> list:
    """Search notes by content, title, or tags.
    
    Args:
        query: Search query string
        folder: Optional folder to search within
    """
    try:
        client = get_vault_client()
        results = client.search_notes(query, folder)
        
        return [
            {
                "path": note['path'],
                "title": note['title'],
                "excerpt": note['content'][:200] + "..." if len(note['content']) > 200 else note['content']
            }
            for note in results
        ]
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
def get_all_tags() -> list:
    """Get all unique tags used in the vault."""
    try:
        client = get_vault_client()
        return client.get_tags()
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
def get_backlinks(path: str) -> list:
    """Find all notes that link to the specified note.
    
    Args:
        path: Path to the target note
    """
    try:
        client = get_vault_client()
        backlinks = client.get_backlinks(path)
        
        return [
            {
                "note_path": link['note']['path'],
                "note_title": link['note']['title'],
                "link_text": link['link_text']
            }
            for link in backlinks
        ]
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
def get_note_links(path: str) -> list:
    """Get all wikilinks from a note.
    
    Args:
        path: Path to the note
    """
    try:
        client = get_vault_client()
        return client.get_note_links(path)
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
def create_folder(folder_path: str) -> dict:
    """Create a new folder in the vault.
    
    Args:
        folder_path: Path to the new folder (e.g., "projects/new-project/")
    """
    try:
        if is_read_only():
            return read_only_error()

        client = get_vault_client()
        success = client.create_folder(folder_path)
        
        if success:
            return {"success": True, "message": f"Folder created: {folder_path}"}
        else:
            return {"error": f"Folder already exists: {folder_path}"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_folder_structure() -> dict:
    """Get the complete folder structure of the vault."""
    try:
        client = get_vault_client()
        return client.get_folder_structure()
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def create_base(
    path: str,
    views: list,
    filters: Optional[Any] = None,
    formulas: Optional[dict] = None,
    properties: Optional[dict] = None,
    summaries: Optional[dict] = None,
) -> dict:
    """Create a new Obsidian Base (.base) file with schema validation.

    Bases are database-like views over your notes. The structure is validated
    against the official Bases schema before anything is written; an invalid
    base is rejected with a message naming the offending path, and no file is
    created.

    Args:
        path: Vault-relative path ending in .base (e.g., "Bases/Tasks.base")
        views: List of view objects (at least one). Each needs a "type"
            (table | list | cards | map) and usually a "name" and "order".
        filters: Optional global filter. Either a string statement or a mapping
            with one of "and" / "or" / "not" holding a list of conditions.
        formulas: Optional mapping of formula name -> expression string.
        properties: Optional mapping of property name -> config (e.g.,
            {"status": {"displayName": "Status"}}).
        summaries: Optional mapping of summary name -> expression string.
    """
    try:
        if is_read_only():
            return read_only_error()

        client = get_vault_client()
        data = build_base(
            views,
            filters=filters,
            formulas=formulas,
            properties=properties,
            summaries=summaries,
        )
        return client.create_base(path, data)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def update_base(
    path: str,
    filters: Optional[Any] = None,
    formulas: Optional[dict] = None,
    properties: Optional[dict] = None,
    summaries: Optional[dict] = None,
    upsert_views: Optional[list] = None,
    remove_views: Optional[list] = None,
    replace_filters: bool = False,
) -> dict:
    """Update an existing .base file with merge semantics, then re-validate.

    The file is read, the requested changes are merged, the whole structure is
    re-validated, the original is backed up (when backup-on-write is enabled),
    and the result is written back.

    Args:
        path: Vault-relative path to the .base file.
        filters: When provided, replaces the global filters wholesale. To
            remove filters entirely, pass replace_filters=true and leave this
            unset.
        formulas: Mapping merged into existing formulas. A null value for a key
            removes that formula.
        properties: Mapping merged into existing properties (null removes).
        summaries: Mapping merged into existing summaries (null removes).
        upsert_views: List of view objects. Each replaces the existing view
            with the same "name", or is appended when there is no match.
        remove_views: List of view names to delete.
        replace_filters: Set true (with filters unset) to remove global filters.
    """
    try:
        if is_read_only():
            return read_only_error()

        client = get_vault_client()
        return client.update_base(
            path,
            filters=filters,
            formulas=formulas,
            properties=properties,
            summaries=summaries,
            upsert_views=upsert_views,
            remove_views=remove_views,
            replace_filters=replace_filters,
        )
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_base(path: str) -> dict:
    """Read a .base file as a parsed structure plus its raw YAML.

    Tolerant of imperfect files: if the YAML cannot be parsed, the raw content
    is returned with a "parse_error" field instead of failing.

    Args:
        path: Vault-relative path to the .base file.
    """
    try:
        client = get_vault_client()
        base = client.get_base(path)
        if base is None:
            return {"error": f"Base not found: {path}"}
        return base
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_bases(folder: Optional[str] = None) -> list:
    """List all .base files in the vault or a folder, with their view names.

    Args:
        folder: Optional folder path to search within (e.g., "Bases/").
    """
    try:
        client = get_vault_client()
        return client.list_bases(folder)
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
def delete_base(path: str) -> dict:
    """Delete a .base file.

    Respects read-only mode and backup-on-write. Only .base files can be
    deleted through this tool.

    Args:
        path: Vault-relative path to the .base file to delete.
    """
    try:
        if is_read_only():
            return read_only_error()

        client = get_vault_client()
        success = client.delete_base(path)

        if success:
            return {"success": True, "message": f"Base deleted: {path}"}
        else:
            return {"error": f"Base not found: {path}"}
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    # For local testing
    mcp.run()
