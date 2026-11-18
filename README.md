# QIDI Q2 Print Bridge

Print models from your phone (and from Bambu Studio) on a **QIDI Q2**.

The bridge is a small program that runs on your Windows PC. It:

1. gives you a **web page you open on your phone** where you pick a model file and tap **Print on Q2**,
2. **re-slices** the model with proper QIDI Q2 settings (using OrcaSlicer or QIDI Studio in the background),
3. **sends it to the Q2** over your home Wi-Fi and starts the print.

Optionally it can also make your PC **show up as a printer inside Bambu Studio**, so you can press Print in
Bambu Studio and the job ends up on the Q2.

## What it can and can't do

| | |
|---|---|
| ✅ Phone → Q2 | Download a model (from MakerWorld, Printables, anywhere), open the bridge page, tap Print. |
| ✅ Bambu Studio → Q2 | The bridge pretends to be a Bambu printer on your home network. |
| ✅ Already-sliced Q2 files | `.gcode.3mf` files sliced for the Q2 in QIDI Studio/OrcaSlicer are sent as-is. |
| ❌ The Print button inside Bambu Handy | Handy only talks to real Bambu printers through Bambu's cloud. It has no local-network mode, so nothing on your network can appear in it. Use Handy/MakerWorld to *find* models, then download and send them with the bridge page. |
| ⚠️ Multi-colour models | Without a QIDI Box they are printed in one colour. With a Box, tick "I have a QIDI Box" in Settings. |
| ⚠️ Bambu-specific tweaks | Re-slicing uses QIDI Q2 settings. Painted supports or per-object tweaks in a Bambu project may be lost. |

## One-time setup (about 10 minutes)

1. **Install Python** from <https://www.python.org/downloads/>. During setup tick **"Add python.exe to PATH"**.
2. **Install a slicer** that knows the Q2: [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer/releases) (free)
   or QIDI Studio. Open it once so it finishes setting up.
3. **Download this project**: on GitHub press the green **Code** button → **Download ZIP**, then unzip it somewhere
   easy, like your Documents folder.
4. Double-click **`Start Bridge.bat`**. If Windows asks whether to allow network access, click **Allow**
   (tick "Private networks").
5. The black window shows an address like `http://192.168.1.23:8765`. Open it **on your phone**
   (the phone must be on the same Wi-Fi as the PC).
6. In **Settings**, type the Q2's IP address (on the Q2's screen: Settings → Network), tap **Test connection**,
   then **Save settings**.

Tip: on your phone use "Add to Home Screen" so the bridge opens like an app.

## Printing from your phone

1. Find a model on MakerWorld (in your phone's browser, or via Handy's share button).
2. Tap **Download** and save the **.3mf** (or .stl).
3. Open the bridge page, tap **Tap to choose a file**, pick the download.
4. Choose quality, plate number (for projects with several plates) and filament, then tap **Print on Q2**.
5. Watch progress under **Recent jobs**. The **Printer** tab shows temperatures and progress.
6. To stop a print, open the **Printer** tab and tap **Pause** (then **Resume**), or **Cancel print** and tap it
   again to confirm. You can also cancel from the Q2's own screen.

The PC has to be on with the bridge window open while it prepares and sends the job. Once the Q2 has started,
you can close it.

## Printing from Bambu Studio (optional)

1. Double-click **`Set up Bambu Studio.bat`** and click **Yes** when Windows asks for permission.
   It adds the bridge's security certificate to Bambu Studio (and OrcaSlicer) so they trust it,
   and prints the **access code** you need.
2. Close Bambu Studio completely, then start the bridge again with `Start Bridge.bat`.
3. Open Bambu Studio. In the **Device** tab, the printer "QIDI Q2 (bridge)" should appear (LAN).
   Connect with the access code shown in the bridge window or on the bridge page under Settings.
   If it doesn't appear, add it by IP address with the same access code.
4. In Bambu Studio, pick **Bambu Lab X1 Carbon** as the printer (that's what the bridge pretends to be), slice, and press **Print**.
   The bridge re-slices the job for the Q2 and starts it. Using **Send** instead of Print just uploads it to the Q2.

Re-run `Set up Bambu Studio.bat` after a Bambu Studio update, because updates replace its certificate file.

## Troubleshooting

- **"Could not reach the printer"**: check the Q2 is on and the IP in Settings is right. The IP can change after
  a router restart; giving the Q2 a fixed IP in your router stops that.
- **"No slicer found"**: install OrcaSlicer, or put the full path to `orca-slicer.exe` in Settings → Slicer program.
- **Phone can't open the page**: make sure the phone is on the home Wi-Fi (not mobile data) and that Windows
  Firewall allowed Python on private networks.
- **Slicing failed**: open the job's **Details** on the Print tab. Sending the plain `.stl` usually works when a
  complicated project doesn't.

## For the technically curious

- Python 3.10+, standard library only for the phone bridge. The Bambu Studio printer also needs `cryptography`.
- Talks to the Q2 through Moonraker (`POST /server/files/upload` with `print=true`) on port 7125.
- Slices with the OrcaSlicer / QIDI Studio command line. Presets are read from the slicer's own profile folders
  (system and user presets), with their inheritance merged so any recent version works. Newer OrcaSlicer builds
  pack their presets (`.opc`); then the bridge downloads the Q2 presets once from the OrcaSlicer project on GitHub
  into `data/profiles`.
- The pretend Bambu printer implements SSDP discovery (UDP 2021), the bind handshake (TCP 3000 / TLS 3002),
  a small MQTT broker (TLS 8883) and implicit FTPS (990, passive ports 50000-50100).
  It uses its own certificate authority stored in `data/certs`.
- Settings and job files live in the `data` folder next to the program.
- Run the tests with `python -m unittest discover -s tests -t .`
