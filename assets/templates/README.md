# Up Next background template

CRTV Sim renders every Up Next screen at 640×480 using a fixed layout so station
art can be designed without colliding with schedule text.

Files:

- `up-next-background-template.png` — usable 640×480 generic on-air background.
- `up-next-background-template-master.png` — high-resolution 4:3 authoring master.
- `up-next-layout-guide.svg` — labeled overlay showing all reserved regions.
- `up-next-layout-example.png` — verified example with generated schedule text.

Layout coordinates at 640×480:

- Station logo zone: `x=220–420`, `y=12–80`.
- Dynamic text safe area: `x=56–584`, `y=92–408`.
- Heading baseline: `y=112`.
- Three-listing baselines: `y=188`, `252`, and `316`.
- Tagline baseline: `y=390`.
- Keep essential artwork at least 8 pixels from the edge for CRT overscan.

Station-specific art may replace the colors, frame, logo plaque, and perimeter
decoration. Keep the central panel quiet and high-contrast. Do not place logos,
faces, mascots, or other important artwork inside the dynamic text safe area.
The labeled SVG is an authoring reference and should not be placed in a channel's
`graphics/up-next` folder.
