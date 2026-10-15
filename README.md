# 🍈 Baqylau.ai — Local AI Proctoring

**[English](README.md) | [Русский](README_RU.md) | [Қазақша](README_KK.md)**

Local online exam monitoring system: smartphone detection (YOLO),
gaze and head tracking (MediaPipe), workspace protection
(hotkey blocking and foreign-window control). All processing stays inside
the institution network — student data never leaves.

Case: Qostanai Industry Hackathon, "Local Proctoring" · Demo Day 16.10.2026.

## Download ready builds

Releases are kept separate — website and app don't mix:

- **Student app (Windows only)** — releases tagged `student-vX`:
  [Releases](https://github.com/dima-mip/baqylau.ai/releases) →
  `BaqylauStudent-win64.zip`: unzip and run `BaqylauStudent.exe`
  (window opens fullscreen immediately).
- **Website/panel (Windows and Linux)** — releases tagged `server-vX`:
  `BaqylauServer-win64.zip`: unzip, install Flask
  (`pip install -r requirements-server.txt`), run `start_server.bat`
  (Windows) or `./start_server.sh` (Linux). First login: `admin / admin123`.

## How it works

- **Student app** (desktop, PySide6): login, camera turns on only after
  the teacher starts the exam. During the exam it shows only the test —
  no camera, no risk, no violation feed.
- **Teacher web panel** (Flask, lightweight): student cards with
  camera thumbnails, risk score, main violations, exam start/stop,
  notifications with photo/video evidence.
- **Admin** sees everything: users, action audit, all evidence
  and results.

## What is detected

| Event | Level |
|---|---|
| Phone in frame / raised to screen (filming) | CRITICAL |
| Gaze down + phone (reading from device) | CRITICAL |
| Head turned for more than 3 s | HIGH |
| Gaze away for more than 4 s | HIGH |
| Second face, leaving exam window, forbidden keys | HIGH/MEDIUM |
| RISK 100 | photo + ~9 s video → notification + locally (+ Drive) |

## Run

**Panel (teacher PC, Windows and Linux):** double-click
`start_server.bat` (Windows) or `./start_server.sh` (Linux),
or manually:
```bash
cd baqylau
python -m server.app   # http://<ip>:5050, first login: admin / admin123
```

**Student (Windows only):** ready-made `BaqylauStudent.exe` — from
[Releases](https://github.com/dima-mip/baqylau.ai/releases) (no Python
required, window opens fullscreen immediately).
Or `start_student.bat` / `BaqylauStudent.vbs`.

Build EXE from source: `build_exe.bat` (10–20 min, internet required
for pip). Models (`yolov8n.pt`, `face_landmarker.task`) are already bundled.

**Student details:** login window (server + login/password or local
mode) → test appears after the teacher starts the exam.

**Console HUD (development):** `python main.py --simulate-eeg`

## Gaze calibration (opt-in)

Disabled by default. 🎯 button — guided 5-point calibration
(center/left/right/up/down, ~8 s): personal zero and thresholds.
`--quick-calib` — fast 2 s. Risk is frozen during calibration.

## Structure

```
baqylau/
├── main_gui.py / main.py   # student app / console HUD
├── server/                 # web panel (Flask + SQLite)
├── core/vision/            # YOLO detector, pose/gaze
├── core/eeg/               # Muse EEG (bonus channel, optional)
├── core/fusion/            # risk engine, sync, alerts
├── core/security/          # key blocking, window control
├── core/net/               # panel client (stdlib)
├── ui/                     # Bento interface, brand, QSS
└── docs/brand.md           # palette and storage map
```

## Where recordings are stored

- Student PC: `data/logs/snapshots/` (photos/videos), `events.jsonl`
- Server: `data/evidence/`, DB `data/baqylau.db`
- Google Drive — optional (`BAQYLAU_DRIVE_FOLDER_ID` + credentials)
