# Bundled fonts

## Anton (Anton.ttf)
- **Licence:** SIL Open Font License, Version 1.1
- **Source:** https://fonts.google.com/specimen/Anton
- **Commercial use:** permitted, including embedding in video
- **Redistribution:** permitted under the OFL

Anton is the default caption face. It is downloaded automatically on first run
if absent (see `engine/video/fonts.py`) and used via ffmpeg's `fontsdir`, so
caption rendering is identical on every machine.

## Oswald (Oswald.ttf, optional)
- **Licence:** SIL Open Font License, Version 1.1
- **Source:** https://fonts.google.com/specimen/Oswald

## Montserrat (MontserratBlack.ttf, optional)
- **Licence:** SIL Open Font License, Version 1.1
- **Source:** https://fonts.google.com/specimen/Montserrat
- **Note:** this is the *variable* font file. libass has limited variable-font
  support, so it is used for thumbnail sub-text rather than captions.

## System fallbacks
If no OFL font can be downloaded, the engine copies a system font (Arial Black,
Impact, Segoe UI Black, DejaVu Sans Bold or Liberation Sans Bold) into this
directory so `fontsdir` stays self-contained.

**These system fonts are NOT redistributable.** They are copied for local
rendering only and are excluded from version control. Do not ship them.
