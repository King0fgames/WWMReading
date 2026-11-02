# WWMReading

Plays the arc meter studying minigame in Where Winds Meet by holding and releasing space to keep the tick inside the gold zone. The gold zone is re-read from the screen, so a different range each session needs no changes.

> **Use at your own risk:** automating a game may break its rules and could get your account banned.

## Setup

You need Windows and Python 3.

```bash
pip install mss numpy
```

## Usage

1. Open the game and start the minigame so the meter is showing.
2. Run the script. If the game ignores the key, run it as administrator.

   ```bash
   python meter_bot.py
   ```

3. On the first run, hover over each point it asks for and press F6: the left end of the arc, the top middle, the right end, a spot in the gold zone, and finally the game window. This is saved to `meter_config.json`.
4. Press F7 to start.

| Key | Action |
|---|---|
| F6 | Calibrate again, if the meter moves on screen |
| F7 | Start or stop |
| F8 | Quit |

These keys work from any window, so pressing them in another app also triggers the bot.

## Auto stop

The minigame closes on the half hour, so the bot releases space and stops at :29:00 and :59:00 by the PC's clock. Restart the minigame and press F7 to carry on.

## Running in the background

The game doesn't need to be focused. Space goes straight to the game window while it's unfocused, so it won't land in another app.

The startup message tells you how the game is being read:

| Message | Meaning |
|---|---|
| `Reading the game window directly...` | The game can be covered or unfocused |
| `The game can't be read directly...` | The game can be unfocused but must stay visible, for example on a second monitor |

## How it works

- **Finding the tick:** it samples along the arc and looks for a tall, narrow mark that sticks out both sides of the meter. Scrolling white text is mostly too short or too wide to count.
- **Staying in the zone:** it trims a margin off each end of the gold, because the real target is narrower than the colour. It presses and releases at set points inside that range rather than spamming the key.
- **Riding out interference:** it learns how fast the tick moves while held and released, then tracks the tick from those speeds. Marks on screen can only correct that estimate slightly, so text passing over the meter can't drag it away.

If the tick stays hidden for 5 seconds, it starts learning again. If the meter disappears for over a second, it forgets the session and picks up the next zone when one appears.

## Settings

Settings are at the top of `meter_bot.py`. These are the ones most likely to need changing:

| Setting | Default | Purpose |
|---|---|---|
| `STOP_MINUTES` | `(29, 59)` | Minutes of each hour to stop at |
| `STOP_SECOND` | `0` | Second within those minutes to stop at |
| `ZONE_MARGIN` | `0.2` | Share of the gold at each end to treat as unsafe |
| `TRIGGER_RANGE` | `0.7` | Share of the safe zone between pressing and releasing; bigger means fewer inputs |
| `KEY_DELAY` | `0.04` | Seconds before the tick responds; raise it if the tick overshoots |
| `TOLERANCE` | `60` | Gold colour matching; raise it if detection is patchy |
| `INVERT` | `False` | Set to `True` if holding space moves the tick right to left |

## Troubleshooting

| Problem | What to do |
|---|---|
| `Windows refused the key` | Run the script as administrator |
| `The tick isn't responding to space` | The game may only accept keys while focused, so keep it focused |
| The tick stops being tracked when the game is covered | The direct read may be returning a frozen picture, so leave the game uncovered |
| The tick isn't tracked after the meter or window moves | Press F6 to calibrate again |

## Known limits

If the tick is fully hidden for several seconds while a tick-sized mark sits near the zone, the tick can drift out of the gold. It picks the tick back up once it's visible again.
