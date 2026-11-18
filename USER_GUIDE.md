# Print Bridge user guide

Print Bridge lets you print models from your phone on your QIDI printer. It runs on your Windows PC. You open
its page on your phone, pick a model, check the colours and tap **Print**. The PC prepares the model with the
right settings for your printer and sends it over your home Wi-Fi.

This guide uses the QIDI Q2 as the example. Other QIDI printers (X-Plus 4, Max 4, Q1 Pro, X-Plus 3 and more)
work the same way.

**Contents**

1. [What you need](#what-you-need)
2. [Setting it up the first time](#setting-it-up-the-first-time)
3. [Every time you want to print](#every-time-you-want-to-print)
4. [Finding models (MakerWorld and Bambu Handy)](#finding-models-makerworld-and-bambu-handy)
5. [Printing a model](#printing-a-model)
6. [Colours and the QIDI Box](#colours-and-the-qidi-box)
7. [Size, copies and settings](#size-copies-and-settings)
8. [Watching, pausing and cancelling a print](#watching-pausing-and-cancelling-a-print)
9. [The filament library](#the-filament-library)
10. [Settings](#settings)
11. [Printing from Bambu Studio (optional)](#printing-from-bambu-studio-optional)
12. [Troubleshooting](#troubleshooting)
13. [Good habits](#good-habits)

## What you need

- A **Windows PC** on the same Wi-Fi as the printer. It has to be on while a job is being prepared and sent.
  Once the printer has started, you can turn the PC off.
- **Python** (free). Install it from python.org and tick **"Add python.exe to PATH"** during setup.
- **OrcaSlicer** (free) or QIDI Studio. Open it once after installing so it finishes setting itself up.
- Your **phone**, on the same Wi-Fi as the PC and the printer.

## Setting it up the first time

1. Unzip the Print Bridge folder somewhere easy, such as Documents.
2. Double-click **Start Bridge.bat**. A black window opens. Leave it open.
3. If Windows asks whether Python may use the network, tick **Private networks** and click **Allow**.
4. The black window shows an address such as `http://192.168.68.54:8765`. Type it into your phone's browser.
5. The page asks you to find your printer:
   - tap **Search my Wi-Fi** and tap your printer when it appears, or
   - type the printer's IP address. On the Q2 it's shown under **Settings → Network** on the printer's screen.
6. Pick your printer model and nozzle (usually 0.4 mm), then tap **Done, let's print**.
7. Optional: in your phone's browser menu, tap **Add to Home Screen**. The bridge then opens like an app.

> **Use your normal home Wi-Fi, not a guest network.** Guest networks keep devices apart, so the phone can't
> reach the PC and the PC can't reach the printer.

## Every time you want to print

1. Turn on the printer.
2. On the PC, double-click **Start Bridge.bat** and leave the black window open.
3. On your phone, open the bridge page (or the Home Screen icon).

The printer's status shows under the title at the top. A green dot means it's ready. Orange means it's
busy printing, and red means the page can't reach it.

## Finding models (MakerWorld and Bambu Handy)

Bambu Handy can't send prints to a QIDI printer, because it only works with Bambu printers. You can still use it
(or MakerWorld, Printables or Thingiverse) to find models:

1. Find a model you like. In Bambu Handy, use the share button to open it in your phone's browser.
2. Tap **Download**. Choose the **.3mf** file if there is one. It keeps the designer's size, colours and plates.
   An .stl only has the shape.
3. Save the file to your phone, in **Downloads**.

> **Google Drive and iCloud files:** if a file is only in Google Drive or iCloud, the phone can't hand it to the
> page and the upload stops. Open it in the Drive or Files app, choose **Download** (or "Save to device"), then
> pick it from **Downloads**.

## Printing a model

The **Print** tab walks you through it in steps.

1. **Choose a model.** Tap the dashed box and pick your file (.3mf, .stl, .obj, .step, .gcode or .zip).
   You see a preview and where the file came from. Files made for a Bambu printer are prepared for your QIDI
   printer automatically.
2. **Plate.** If a project has several plates, pick the one you want.
3. **Colours.** See [Colours and the QIDI Box](#colours-and-the-qidi-box).
4. **Print.** Choose the quality, size, copies and any other settings, then tap **Print on Q2**.

Progress shows under **Recent jobs**: *Checking*, then *Preparing* (this can take a minute or two), then
*Sending*, then *Done*. Tap **Details** on a job to see exactly what happened.

Tick off **Start printing straight away** if you only want to send the file and start it from the printer later.

## Colours and the QIDI Box

The bridge reads your QIDI Box: what's in each slot, its colour and material, which slot is in the print head,
and the Box's humidity.

**When you print:**

- Each colour of the model gets the Box slot with the closest colour of the same material. Tap a different slot
  to change it.
- A plain .stl has no colour, so it uses the filament that's in the print head now.
- To print a multi-colour model in a single colour, pick the same slot for every colour.
- The bridge picks the right filament settings (temperatures and speeds) for whatever is in each slot.
- The Q2 switches the Box on or off for each print it starts from its own screen. The bridge switches it on for
  every print that uses Box colours, so you don't need to do anything.

**Changing the Box from your phone** (on the **Printer** tab, tap a slot):

- **Filament and colour:** choose from the same list as the printer's screen, then tap **Save to printer** and
  tap again to confirm.
- **Load into print head** or **Unload:** loads or unloads that slot. You can't do this during a print.
- **Read the spool's RFID tag:** for QIDI spools with a tag.

Every button needs a second tap, so nothing changes by accident. Drying isn't on the page yet; start it on the
printer's screen.

## Size, copies and settings

These are in step 3 (**Print**) once you have picked a model.

- **Quality (layer height):** *0.20mm Standard* is the best everyday choice. Smaller numbers give finer detail
  but take longer. Bigger numbers are faster and rougher.
- **Size:** drag the slider or tap 50%, 150%, 200% or 300%. The page shows the new size in centimetres and
  warns you if it won't fit on the bed.
- **Copies:** tap **+** to print several at once. They're spread out on the bed, and the page warns you if
  they won't fit.
- **More settings:**
  - **Strength (infill):** how solid the inside is. *Normal* is fine for most things. Choose *Strong* or more for
    parts that have to take force.
  - **Walls:** more walls make a part stronger and more watertight.
  - **Supports:** turn on for models with parts hanging in the air. *Tree* supports are easiest to remove.
  - **Brim:** a thin skirt around the first layer that helps prints stick. *Automatic* adds one when it's
    needed.

*Normal* always means QIDI's own tested settings for the quality you picked.

**Bed sticking:** in Settings, make sure **Bed plate** matches the plate on your printer (the Q2 comes with
the gold/black *Textured PEI* plate). Leave **Extra bed grip** on. It slows the first layer down, keeps the fan
off for the first three layers and warms the first layer a little.

## Watching, pausing and cancelling a print

On the **Printer** tab you can see the progress, time left, and nozzle and bed temperatures.

- **Pause**, then **Resume** to carry on.
- **Cancel print**, then tap it again to confirm. You can also cancel from the printer's screen.

While something is printing, a **Printing now** card also appears on the Print tab.

## The filament library

The **Filaments** tab lists every filament your printer has tested settings for (QIDI, Generic, Polymaker,
Bambu, HATCHBOX, Overture and more).

- Tap a material (PLA, PETG, ABS…) or search by name.
- Tap a filament to see its nozzle and bed temperatures, fan and drying temperature.
- **Use this when there's no Box** makes it the filament the bridge uses when the Box isn't used.
- At the top, **In your QIDI Box** shows the settings used for each slot.

## Settings

- **Your printer:** the model, the nozzle and the IP address. Use **Search my Wi-Fi** if the address has
  changed.
- **Use my QIDI Box:** turn this off to ignore the Box and print with one filament.
- **Filament when there's no Box**, **Normal quality**, **Bed plate** and **Extra bed grip**.
- **Advanced:** the slicer program location, the printer port and a PIN to lock the page.
- **Bambu Studio printer:** see the next section.

Tap **Save settings** when you're done.

## Printing from Bambu Studio (optional)

The bridge can make your PC show up as a printer inside Bambu Studio on the PC.

1. Double-click **Set up Bambu Studio.bat** and click **Yes** when Windows asks.
2. In the bridge's **Settings**, tick **Show up as a printer in Bambu Studio** and save.
3. Close Bambu Studio and the bridge window, then start the bridge again.
4. In Bambu Studio's **Device** tab, connect to **QIDI Q2 (bridge)** with the access code shown in Settings.
5. Pick **Bambu Lab X1 Carbon** as the printer, slice and press **Print**. The bridge prepares the job for your
   QIDI printer and starts it.

Run **Set up Bambu Studio.bat** again after Bambu Studio updates.

## Troubleshooting

**The phone says it can't reach the bridge.**
Check the black window is open on the PC, the phone is on your home Wi-Fi (not mobile data or a guest
network), and Windows allowed Python on private networks. Use the address shown in the black window.

**"Could not reach the printer".**
Make sure the printer is on. Then go to **Settings → Search my Wi-Fi**, because the printer's address can
change after the router restarts. Giving the printer a fixed address in your router stops this.

**The upload stops or says it was interrupted.**
The file is probably in Google Drive or iCloud. Download it to the phone first, then pick it from Downloads.

**The print doesn't stick to the bed.**
Check **Settings → Bed plate** matches your plate and **Extra bed grip** is on. Wipe the plate with warm water
and a drop of dish soap (fingerprints stop prints sticking). For small or tall parts, set **Brim** to
*Always*.

**The print came out smaller (or bigger) than expected.**
The bridge prints the size saved in the file. Many .stl downloads are small; the designer's .3mf usually keeps
the right size. Use the **Size** slider to change it.

**Only one colour printed.**
Check the colours step on the Print tab: each colour needs its own slot. Make sure **Use my QIDI Box** is on in
Settings.

**The Box shows the wrong filament or colour.**
Tap the slot on the Printer tab and set it, or set it on the printer's screen.

**"No slicer found".**
Install OrcaSlicer and open it once, or put the path to `orca-slicer.exe` in **Settings → Advanced**.

**Preparing (slicing) failed.**
Open **Details** on the job. Sending the plain .stl often works when a complicated project doesn't.

**Still stuck?** Close the black window, start **Start Bridge.bat** again, and refresh the page. The file
`data\bridge.log` in the bridge folder records what happened.

## Good habits

- Keep the bridge window open until the job says **Done**.
- Download **.3mf** files rather than .stl when there's a choice.
- Store filament dry. The Printer tab shows the Box humidity; above about 40% it's worth drying PLA, and PETG
  and nylon need it sooner.
- Keep the plate clean, and don't touch the print surface with your fingers.
