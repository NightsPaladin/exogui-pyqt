# eXoDOS GUI Themes

Themes are simple JSON files. Drop any `.json` file in this directory and it will appear in **View → Theme**.

## Creating a Custom Theme

Copy any existing `.json` file, rename it (e.g. `my-theme.json`), and edit the colors:

```json
{
  "name": "My Theme",
  "bg_window":  "#1c1c1e",
  "bg_panel":   "#252523",
  "bg_card":    "#323230",
  "bg_input":   "#2c2c2a",
  "bg_status":  "#141412",
  "border":     "#48484a",
  "handle":     "#3a3a38",
  "accent":     "#4a90d9",
  "text_hi":    "#f2f2f0",
  "text_med":   "#8e8e8a",
  "text_lo":    "#636360",
  "green":      "#30d158",
  "orange":     "#ff9f0a"
}
```

## Color Fields

| Field | Used for |
|-------|----------|
| `bg_window` | Deepest background (window, box art area) |
| `bg_panel` | Panel / list / scroll area background |
| `bg_card` | Alternate row / card background |
| `bg_input` | Text inputs, combo boxes, buttons |
| `bg_status` | Menu bar and status bar |
| `border` | Widget borders and separators |
| `handle` | Splitter handle |
| `accent` | Selection highlight, primary buttons, links |
| `text_hi` | Primary / high-emphasis text |
| `text_med` | Secondary / caption text |
| `text_lo` | Dim / tertiary text, counts |
| `green` | "Installed" indicator |
| `orange` | Warnings, emulator badge |

Restart the app (or use **View → Theme** to reselect) to pick up new files.
