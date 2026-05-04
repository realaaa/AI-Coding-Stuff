#!/usr/bin/env python3
"""
Convert a RoyalTSX export (.rtsz / XML) to an electerm bookmarks JSON file.

Usage:
    python3 rtsz_to_electerm.py <input.rtsz> <output.json>

Notes:
- RoyalTSX encrypts passwords in the export using the master password; they are
  NOT decrypted by this script. Use SSH agent / keys / re-enter passwords in
  electerm after import.
- Connection types:
    RoyalSSHConnection           -> electerm type "ssh"
    RoyalFileTransferConnection  -> electerm type "ssh" (SFTP pane is built-in)
    RoyalRDSConnection           -> electerm type "rdp"
- Folder hierarchy under the root "Connections" folder is preserved as
  electerm bookmarkGroups. The root wrapper plus "Credentials" and "Tasks"
  folders are skipped.
"""

import json
import random
import string
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

ID_ALPHABET = string.ascii_letters + string.digits + "_"


def gen_id(used: set) -> str:
    """Generate a unique 7-character electerm-style ID."""
    while True:
        new_id = "".join(random.choices(ID_ALPHABET, k=7))
        if new_id not in used:
            used.add(new_id)
            return new_id


def text_of(elem, tag, default=""):
    """Return text of a child element, or `default` if missing/empty."""
    if elem is None:
        return default
    sub = elem.find(tag)
    if sub is None or sub.text is None:
        return default
    return sub.text


def to_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #

# Folders that contain things we don't want to import as bookmark groups.
SKIP_FOLDER_NAMES = {"Credentials", "Tasks", "Trashcan"}


def convert(rtsz_path: str) -> dict:
    tree = ET.parse(rtsz_path)
    root = tree.getroot()

    # ---- Index everything by RoyalTSX UUID ---------------------------------
    folders = {}        # rtsz_id -> {name, parent_id}
    credentials = {}    # rtsz_id -> {username}
    connections = []    # list of dicts (raw, pre-mapping)
    royal_doc_id = None

    for child in root:
        tag = child.tag
        rid = text_of(child, "ID")
        if tag == "RoyalDocument":
            royal_doc_id = rid
        elif tag == "RoyalFolder":
            folders[rid] = {
                "name": text_of(child, "Name"),
                "parent_id": text_of(child, "ParentID") or None,
            }
        elif tag == "RoyalCredential":
            credentials[rid] = {
                "username": text_of(child, "UserName"),
            }
        elif tag in ("RoyalSSHConnection",
                     "RoyalFileTransferConnection",
                     "RoyalRDSConnection"):
            connections.append({
                "rtsz_id": rid,
                "kind": tag,
                "name": text_of(child, "Name"),
                "host": text_of(child, "URI"),
                "port": text_of(child, "Port") or text_of(child, "RDPPort"),
                "username": text_of(child, "CredentialUsername"),
                "credential_id": text_of(child, "CredentialId"),
                "is_telnet": text_of(child, "IsTelnetConnection") == "True",
                "is_serial": text_of(child, "IsSerialPortConnection") == "True",
                "parent_id": text_of(child, "ParentID") or None,
            })

    # ---- Decide which folders become electerm groups -----------------------
    # Skip:
    #   - the root RoyalDocument id
    #   - the top "Connections" wrapper (just an export root)
    #   - "Credentials", "Tasks", "Trashcan"
    root_connections_folder_id = None
    for fid, f in folders.items():
        if f["name"] == "Connections" and f["parent_id"] == royal_doc_id:
            root_connections_folder_id = fid
            break

    folders_to_export = {}
    for fid, f in folders.items():
        if fid == root_connections_folder_id:
            continue
        if f["name"] in SKIP_FOLDER_NAMES:
            continue
        # Only keep folders that descend from the root "Connections" folder
        # (so the "Credentials" subtree / "Tasks" subtree are excluded too).
        anc = f["parent_id"]
        belongs = False
        while anc:
            if anc == root_connections_folder_id:
                belongs = True
                break
            parent = folders.get(anc)
            if not parent:
                break
            anc = parent["parent_id"]
        if belongs:
            folders_to_export[fid] = f

    # ---- Allocate electerm IDs --------------------------------------------
    used_ids = {"default"}
    rtsz_to_eid_group = {}
    for fid in folders_to_export:
        rtsz_to_eid_group[fid] = gen_id(used_ids)
    rtsz_to_eid_bookmark = {}
    for c in connections:
        rtsz_to_eid_bookmark[c["rtsz_id"]] = gen_id(used_ids)

    # ---- Build bookmarks ---------------------------------------------------
    bookmarks = []
    group_to_bookmarks = defaultdict(list)
    orphan_bookmarks = []   # connections whose parent is not an exported group

    skipped = []  # (name, reason)

    for c in connections:
        # Username: prefer cached value on the connection; fall back to credential record.
        username = c["username"]
        if not username and c["credential_id"]:
            cred = credentials.get(c["credential_id"])
            if cred:
                username = cred["username"]

        host = c["host"]
        if not host:
            skipped.append((c["name"], "missing host/URI"))
            continue

        kind = c["kind"]
        bm_id = rtsz_to_eid_bookmark[c["rtsz_id"]]

        if kind == "RoyalRDSConnection":
            bookmark = {
                "id": bm_id,
                "title": c["name"],
                "host": host,
                "username": username or "",
                "password": "",
                "port": to_int(c["port"], 3389),
                "type": "rdp",
            }
        else:
            # SSH or FileTransfer (both -> ssh; SFTP pane is bundled)
            # In RoyalTSX, telnet rides on the SSH connection element with a flag.
            conn_type = "ssh"
            if c["is_telnet"]:
                conn_type = "telnet"
            elif c["is_serial"]:
                conn_type = "serial"

            bookmark = {
                "id": bm_id,
                "title": c["name"],
                "host": host,
                "username": username or "",
                "authType": "password",
                "port": to_int(c["port"], 22),
                "useSshAgent": True,
                "sshAgent": "",
                "runScripts": [{"delay": 500, "script": ""}],
                "envLang": "en_US.UTF-8",
                "encode": "utf-8",
                "type": conn_type,
                "enableSsh": True,
                "enableSftp": True,
                "term": "xterm-256color",
                "displayRaw": False,
                "cipher": [],
                "serverHostKey": [],
                "sshTunnels": [],
                "connectionHoppings": [],
                "quickCommands": [],
            }
        bookmarks.append(bookmark)

        # Route into a group
        parent_eid = rtsz_to_eid_group.get(c["parent_id"])
        if parent_eid:
            group_to_bookmarks[parent_eid].append(bm_id)
        else:
            orphan_bookmarks.append(bm_id)

    # ---- Build bookmarkGroups ---------------------------------------------
    # Always include the "default" group first, like electerm's own export.
    bookmark_groups = [{
        "id": "default",
        "title": "default",
        "bookmarkIds": orphan_bookmarks,
        "bookmarkGroupIds": [],
    }]

    # Then a group for every folder we kept, with parent linkage.
    for fid, f in folders_to_export.items():
        eid = rtsz_to_eid_group[fid]
        # children = subfolders of this folder that we also exported
        child_group_ids = [
            rtsz_to_eid_group[child_fid]
            for child_fid, child_f in folders_to_export.items()
            if child_f["parent_id"] == fid
        ]
        bookmark_groups.append({
            "id": eid,
            "title": f["name"],
            "bookmarkIds": group_to_bookmarks.get(eid, []),
            "bookmarkGroupIds": child_group_ids,
        })

    out = {
        "bookmarkGroups": bookmark_groups,
        "bookmarks": bookmarks,
    }

    # ---- Print a human-readable summary to stderr -------------------------
    summary_lines = [
        f"Folders exported as groups: {len(folders_to_export)}",
        f"Bookmarks written:          {len(bookmarks)}",
        f"  SSH/Telnet/SFTP:          "
        f"{sum(1 for b in bookmarks if b['type'] in ('ssh', 'telnet', 'serial'))}",
        f"  RDP:                      {sum(1 for b in bookmarks if b['type'] == 'rdp')}",
        f"  Orphan (in 'default'):    {len(orphan_bookmarks)}",
    ]
    if skipped:
        summary_lines.append(f"Skipped ({len(skipped)}):")
        for name, reason in skipped:
            summary_lines.append(f"  - {name}: {reason}")
    no_user = [b for b in bookmarks if not b.get("username")]
    if no_user:
        summary_lines.append(
            f"Bookmarks with no resolvable username ({len(no_user)}):"
        )
        for b in no_user:
            summary_lines.append(f"  - {b['title']}")

    print("\n".join(summary_lines), file=sys.stderr)

    return out


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    in_path, out_path = sys.argv[1], sys.argv[2]
    data = convert(in_path)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
