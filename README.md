# QIDI Print Bridge

Print models from your phone (and from Bambu Studio) on a **QIDI printer**: Q2, Q2C, X-Plus 4, X-Max 4,
Q1 Pro, X-Plus 3, X-Max 3, X-Smart 3 and the other QIDI printers that run Klipper. It reads the
**QIDI Box** so multi-colour models print in the colours you have loaded.

The bridge is a small program that runs on your Windows PC. It:

1. gives you a **web page you open on your phone** where you pick a model file, check the colours and tap **Print**,
2. **re-slices** the model with the right settings for your QIDI printer (using OrcaSlicer or QIDI Studio in the
   background), with the filament settings that match what is in each Box slot,
3. **sends it to the printer** over your home Wi-Fi, tells the Box which slot to use for each colour, and starts the print.

Optionally it can also make your PC **show up as a printer inside Bambu Studio**, so you can press Print in
Bambu Studio and the job ends up on your QIDI printer.

## What it can and can't do

| | |
|---|---|
| ✅ Phone → printer | Download a model (from MakerWorld, Printables, anywhere), open the bridge page, tap Print. |
| ✅ QIDI Box colours | The page shows what's in each slot and suggests a slot for every colour of the model. Tap to change. |
| ✅ Bambu Studio → printer | The bridge pretends to be a Bambu printer on your home network. |
| ✅ Already-sliced files | `.gcode.3mf` files sliced for your printer in QIDI Studio/OrcaSlicer are sent as-is. |
| ❌ The Print button inside Bambu Handy | Handy only talks to real Bambu printers through Bambu's cloud. It has no local-network mode, so nothing on your network can appear in it. Use Handy/MakerWorld to *find* models, then download and send them with the bridge page. |
| ⚠️ Multi-colour models | Without a QIDI Box they are printed in one colour. |
| ⚠️ Bambu-specific tweaks | Re-slicing uses QIDI settings. Painted supports or per-object tweaks in a Bambu project may be lost. |

## One-time setup (about 10 minutes)

1. **Install Python** from <https://www.python.org/downloads/>. During setup tick **"Add python.exe to PATH"**.
2. **Install a slicer** that knows QIDI printers: [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer/releases) (free)
   or QIDI Studio. Open it once so it finishes setting up.
3. **Download this project**: on GitHub press the green **Code** button → **Download ZIP**, then unzip it somewhere
   easy, like your Documents folder.
4. Double-click **`Start Bridge.bat`**. If Windows asks whether to allow network access, click **Allow**
   (tick "Private networks").
5. The black window shows an address like `http://192.168.1.23:8765`. Open it **on your phone**
   (the phone must be on the same Wi-Fi as the PC).
6. The first time, the page helps you set up: tap **Search my Wi-Fi** to find the printer (or type its IP address,
   shown on the printer's screen under Settings → Network), pick your QIDI model, and tap **Done**.

Tip: on your phone use "Add to Home Screen" so the bridge opens like an app.

## Printing from your phone

1. Find a model on MakerWorld (in your phone's browser, or via Handy's share button).
2. Tap **Download** and save the **.3mf** (or .stl).
3. Open the bridge page, tap **Tap to choose a file**, pick the download. You see a preview and where it came from.
4. **Colours**: with a QIDI Box, every colour of the model gets a Box slot (the closest colour of the same material).
   Tap another slot to change it. A plain .stl uses the filament that is in the print head now.
   Picking the same slot for every colour prints the model in one colour.
5. Choose the quality (and the plate for projects with several plates). **Size** shows how big the model will
   print; drag it or tap 150% / 200% to make it bigger. Then tap **Print**.
   The bed plate (Settings → Bed plate) sets the bed temperature. **Extra bed grip** (on by default) slows the
   first layer down, keeps the fan off for the first 3 layers and warms the first layer a little, so prints stick.
6. Watch progress under **Recent jobs**. The **Printer** tab shows temperatures, progress, and what is in each Box slot
   (colour, material, which one is in the print head, and the Box humidity).
7. To stop a print, open the **Printer** tab and tap **Pause** (then **Resume**), or **Cancel print** and tap it
   again to confirm. You can also cancel from the printer's own screen.

The PC has to be on with the bridge window open while it prepares and sends the job. Once the printer has started,
you can close it.

## Changing the Box from your phone

On the **Printer** tab, tap a Box slot. You can:

- set the **filament and colour** in that slot (the same choices as the printer's screen), then tap
  **Save to printer** twice,
- **Load into print head** or **Unload** that slot (not while printing),
- **Read the spool's RFID tag** for QIDI spools.

Every button needs a second tap to confirm. The bridge uses the same commands as QIDI Studio
(`UPDATE_FILAMENT_INFORMATION`, `LOAD_FILAMENT_CONTROL`, `UNLOAD_FILAMENT_CONTROL`, `READ_RFID`).
The Q2 switches the Box on or off for each print it starts; the bridge switches it on for every print that uses
Box slots. Drying is not on the page yet; start it on the printer's screen.

## Filament library

The **Filaments** tab lists every filament your printer has tested settings for (QIDI, Generic, Polymaker,
Bambu and more), grouped by material. Search or tap a material, then tap a filament to see its nozzle and bed
temperatures, fan and drying temperature. **Use this when there's no Box** makes it the default. At the top you
see the settings the bridge uses for each Box slot.

## Printing from Bambu Studio (optional)

1. Double-click **`Set up Bambu Studio.bat`** and click **Yes** when Windows asks for permission.
   It adds the bridge's security certificate to Bambu Studio (and OrcaSlicer) so they trust it,
   and prints the **access code** you need.
2. Close Bambu Studio completely, then start the bridge again with `Start Bridge.bat`.
3. Open Bambu Studio. In the **Device** tab, the printer "QIDI Q2 (bridge)" should appear (LAN).
   Connect with the access code shown in the bridge window or on the bridge page under Settings.
   If it doesn't appear, add it by IP address with the same access code.
4. In Bambu Studio, pick **Bambu Lab X1 Carbon** as the printer (that's what the bridge pretends to be), slice, and press **Print**.
   The bridge re-slices the job for your QIDI printer, picks Box slots by colour, and starts it. Using **Send** instead
   of Print just uploads it.

Re-run `Set up Bambu Studio.bat` after a Bambu Studio update, because updates replace its certificate file.

## Troubleshooting

- **"Could not reach the printer"**: check the printer is on and the IP in Settings is right (or tap
  **Search my Wi-Fi**). The IP can change after a router restart; giving the printer a fixed IP in your router stops that.
- **Box colours look wrong**: the bridge reads what the printer's screen says is in each slot. Set the colour and
  material for the slot on the printer, then reopen the page.
- **Another QIDI printer**: pick it in Settings → Printer. The first time, the bridge downloads that printer's
  settings from the OrcaSlicer project (needs internet, takes about 10 seconds).
- **"No slicer found"**: install OrcaSlicer, or put the full path to `orca-slicer.exe` in Settings → Slicer program.
- **Phone can't open the page**: make sure the phone is on the home Wi-Fi (not mobile data) and that Windows
  Firewall allowed Python on private networks.
- **"Upload was interrupted" on Android**: the file is probably in Google Drive. Open it in the Drive app,
  tap ⋮ → Download, then pick it from **Downloads** on the bridge page.
- **Slicing failed**: open the job's **Details** on the Print tab. Sending the plain `.stl` usually works when a
  complicated project doesn't.

## For the technically curious

- Python 3.10+, standard library only for the phone bridge. The Bambu Studio printer also needs `cryptography`.
- Talks to the printer through Moonraker (`POST /server/files/upload` with `print=true`) on port 7125.
- The QIDI Box is read from Klipper's `box_extras` object (falling back to `save_variables` and
  `officiall_filas_list.cfg` on older firmware). Before a print starts, the bridge sets the tool → slot map with
  `SAVE_VARIABLE VARIABLE=value_t<n> VALUE="'slot<m>'"`, the same way OrcaSlicer's QIDI integration does.
- Slices with the OrcaSlicer / QIDI Studio command line. Presets are read from the slicer's own profile folders
  (system and user presets), with their inheritance merged so any recent version works. Newer OrcaSlicer builds
  pack their presets (`.opc`); then the bridge downloads the chosen printer's presets once from the OrcaSlicer
  project on GitHub into `data/profiles`.
- The pretend Bambu printer implements SSDP discovery (UDP 2021), the bind handshake (TCP 3000 / TLS 3002),
  a small MQTT broker (TLS 8883) and implicit FTPS (990, passive ports 50000-50100).
  It uses its own certificate authority stored in `data/certs`.
- Settings and job files live in the `data` folder next to the program.
- Run the tests with `python -m unittest discover -s tests -t .`
