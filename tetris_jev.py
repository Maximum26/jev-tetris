"""Tetris played by Jev (System 1). The window shows Jev's scores for each placement.

The piece starts falling at once. While Jev thinks it keeps drifting down; when the
answer arrives the piece rotates and slides to the chosen spot on the fly, then drops.

API key: put it in a file named .env next to this script:
      TYPESAFE_API_KEY=your_key
(or set the TYPESAFE_API_KEY environment variable).

Run:  python tetris_jev.py
Keys: R = restart (resets the piece counter), ESC = quit

The game stops by itself after MAX_PIECES pieces (counted across all games in one
session), so it cannot burn through your API balance while unattended.
"""
import math
import os
import random
import threading
import time

import pygame
import requests

MAX_PIECES = 500  # hard stop: one API request per piece


def load_api_key():
    """Read TYPESAFE_API_KEY from the environment or from a .env file next to this script."""
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key.strip()
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("TYPESAFE_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


API_URL = "https://api.typesafe.ai/v1/systemone"
API_KEY = load_api_key()
MODEL = "jev-latest"

W, H, CELL = 10, 20, 30
OX, OY = 20, 20
PANEL_X = OX + W * CELL + 30
WIN_W, WIN_H = PANEL_X + 440, OY * 2 + H * CELL

THINK_SPEED = 5    # cells/s while waiting for Jev
THINK_MAX_Y = 6    # the piece hovers no lower than this row while waiting
STEER_SPEED = 12   # cells/s while rotating and sliding
STEP_TIME = 0.05   # seconds per rotate/move step
DROP_SPEED = 60    # cells/s for the final drop

SHAPES = {
    "I": [(0, 0), (1, 0), (2, 0), (3, 0)],
    "O": [(0, 0), (1, 0), (0, 1), (1, 1)],
    "T": [(0, 0), (1, 0), (2, 0), (1, 1)],
    "S": [(1, 0), (2, 0), (0, 1), (1, 1)],
    "Z": [(0, 0), (1, 0), (1, 1), (2, 1)],
    "J": [(0, 0), (0, 1), (1, 1), (2, 1)],
    "L": [(2, 0), (0, 1), (1, 1), (2, 1)],
}
COLORS = {
    "I": (80, 200, 230), "O": (240, 220, 80), "T": (170, 100, 220),
    "S": (100, 210, 100), "Z": (230, 80, 80), "J": (80, 110, 230), "L": (240, 160, 70),
}


# ---------- game logic ----------
def norm(cells):
    mx = min(x for x, _ in cells)
    my = min(y for _, y in cells)
    return sorted((x - mx, y - my) for x, y in cells)


def rotations(name):
    cells, seen, out = norm(SHAPES[name]), set(), []
    for _ in range(4):
        key = frozenset(cells)
        if key not in seen:
            seen.add(key)
            out.append(cells)
        cells = norm([(-y, x) for x, y in cells])
    return out


def fits(board, cells, ox, oy):
    for x, y in cells:
        bx, by = x + ox, y + oy
        if bx < 0 or bx >= W or by >= H or (by >= 0 and board[by][bx]):
            return False
    return True


def apply_piece(board, final, name):
    g = [row[:] for row in board]
    for x, y in final:
        g[y][x] = name
    keep = [row for row in g if not all(row)]
    cleared = H - len(keep)
    return [[0] * W for _ in range(cleared)] + keep, cleared


def board_stats(g):
    heights, holes = [], 0
    for x in range(W):
        top = next((y for y in range(H) if g[y][x]), H)
        heights.append(H - top)
        holes += sum(1 for y in range(top, H) if not g[y][x])
    bump = sum(abs(heights[i] - heights[i + 1]) for i in range(W - 1))
    return holes, max(heights), bump


def extra_stats(g):
    """Almost-full rows (8-9 of 10 filled) and deep wells (depth >= 3, walls count as high)."""
    almost = sum(1 for row in g if 8 <= sum(1 for c in row if c) <= 9)
    hs = [H - next((y for y in range(H) if g[y][x]), H) for x in range(W)]
    depths = [min(hs[x - 1] if x > 0 else H, hs[x + 1] if x < W - 1 else H) - hs[x] for x in range(W)]
    deep = [d for d in depths if d >= 3]
    return almost, len(deep), max(deep, default=0)


def get_placements(board, name):
    res, seen = [], set()
    holes0, _, _ = board_stats(board)
    for r, cells in enumerate(rotations(name)):
        w = max(x for x, _ in cells) + 1
        for c in range(W - w + 1):
            if not fits(board, cells, c, 0):
                continue
            y = 0
            while fits(board, cells, c, y + 1):
                y += 1
            final = frozenset((x + c, yy + y) for x, yy in cells)
            if final in seen:
                continue
            seen.add(final)
            g, lines = apply_piece(board, final, name)
            holes, max_h, bump = board_stats(g)
            almost, wells, deepest = extra_stats(g)
            res.append({
                "id": f"M{len(res) + 1:02d}", "rot": r, "col": c, "y": y, "cells": cells,
                "final": final, "lines": lines, "holes": holes, "max_h": max_h, "bump": bump,
                "dholes": holes - holes0, "grid": g,
                "almost": almost, "wells": wells, "deepest": deepest,
            })
    return res


def ascii_board(g):
    """Board as text rows: '#' filled, '.' empty. First row = highest filled row."""
    top = next((y for y in range(H) if any(g[y])), H - 1)
    return ["".join("#" if c else "." for c in g[y]) for y in range(top, H)]


def column_heights(g):
    return [H - next((y for y in range(H) if g[y][x]), H) for x in range(W)]


def add_lookahead(options, nxt):
    """For every move: best case for the NEXT piece (most lines it can clear, fewest holes it adds)."""
    for o in options:
        follow = get_placements(o["grid"], nxt)
        o["n_lines"] = max((f["lines"] for f in follow), default=None)
        o["n_holes"] = min((f["dholes"] for f in follow), default=None)


def describe(o):
    if o.get("n_lines") is None:
        nxt = "next piece would have no room"
    else:
        nxt = f"next piece best case: clears up to {o['n_lines']} lines, adds at least {o['n_holes']} holes"
    return (f"rotation {o['rot']}, column {o['col']}: clears {o['lines']} lines, "
            f"{o['dholes']:+d} holes (total {o['holes']}), max height {o['max_h']}, "
            f"bumpiness {o['bump']}, almost-full rows {o['almost']}, "
            f"deep wells {o['wells']} (deepest {o['deepest']}); {nxt}")


# ---------- Jev ----------
def ask_jev(options, name, nxt, board, out):
    """One `choice` question with ALL placements. Jev returns a probability for each."""
    t0 = time.time()
    try:
        if not API_KEY:
            raise RuntimeError("TYPESAFE_API_KEY is not set")
        add_lookahead(options, nxt)  # in the worker thread, so the window stays smooth
        body = {
            "model": MODEL,
            "state": {
                "game": "Tetris, 10 columns",
                "current_piece": name,
                "next_piece": nxt,
                "board_legend": "# filled, . empty, first row is the highest filled row, "
                                "columns 0-9 left to right",
                "board": ascii_board(board),
                "column_heights": column_heights(board),
                "holes_now": board_stats(board)[0],
            },
            "questions": {"best_move": {
                "type": "choice",
                "instructions": "Which placement is best for a Tetris player? Avoid holes, "
                                "keep the stack low and flat, clear lines when it is safe. "
                                "Also use the next-piece information: prefer moves that keep deep wells open "
                                "for I pieces, build almost-full rows and leave a good place for next_piece.",
                "criteria": {o["id"]: describe(o) for o in options},
            }},
        }
        r = requests.post(API_URL, json=body, timeout=20,
                          headers={"Authorization": "Bearer " + API_KEY})
        r.raise_for_status()
        a = r.json()["answers"]["best_move"]
        ids = {o["id"] for o in options}
        if a["choice"] not in ids:
            raise RuntimeError("unknown choice " + str(a["choice"]))
        probs = {k: v for k, v in (a.get("probabilities") or {}).items() if k in ids and v > 0}
        total = sum(probs.values())  # API rounds to 0.01, so renormalize to exactly 1
        probs = {k: v / total for k, v in probs.items()} if total > 0 else {a["choice"]: 1.0}
        out.update(choice=a["choice"], probs=probs, conf=a.get("confidence"), mode="Jev", err=None)
    except Exception as e:  # keep the game running even if the API fails
        out.update(choice=random.choice(options)["id"], probs={}, conf=None,
                   mode="random (API error)", err=str(e)[:90])
    out["ms"] = int((time.time() - t0) * 1000)
    out["done"] = True


# ---------- drawing ----------
def draw_cell(screen, x, y, color, outline=False):
    rect = pygame.Rect(OX + x * CELL, OY + int(y * CELL), CELL - 1, CELL - 1)
    if outline:
        pygame.draw.rect(screen, color, rect, 2)
    else:
        pygame.draw.rect(screen, color, rect)


def percents(probs):
    """Whole percents that add up to exactly 100 (largest remainder rounding)."""
    raw = {k: v * 100 for k, v in probs.items()}
    out = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)[:100 - sum(out.values())]:
        out[k] += 1
    return out


def draw_next(screen, small, name):
    """Next piece preview in a simple frame, top right corner of the panel."""
    c = 22
    bw, bh = 120, 86
    bx, by = WIN_W - 20 - bw, 20
    pygame.draw.rect(screen, (170, 170, 180), (bx, by, bw, bh), 2)
    label = small.render("NEXT", True, (170, 170, 170))
    screen.blit(label, (bx + (bw - label.get_width()) // 2, by + 6))
    cells = rotations(name)[0]
    pw = max(x for x, _ in cells) + 1
    ph = max(y for _, y in cells) + 1
    px = bx + (bw - pw * c) // 2
    py = by + 26 + (bh - 26 - ph * c) // 2
    for x, y in cells:
        pygame.draw.rect(screen, COLORS[name], (px + x * c, py + y * c, c - 1, c - 1))


def draw_panel(screen, fonts, st, options):
    mid, small = fonts[1], fonts[2]
    x = PANEL_X
    screen.blit(small.render(f"pieces: {st['pieces']}   lines: {st['lines']}", True, (170, 170, 170)), (x, 24))
    if st.get("next") and st["phase"] != "limit":
        draw_next(screen, small, st["next"])
    out = st["out"]
    if st["phase"] == "limit":
        screen.blit(mid.render(f"Limit reached: {MAX_PIECES} pieces", True, (240, 200, 90)), (x, 60))
        screen.blit(small.render("Stopped to save API balance.", True, (170, 170, 170)), (x, 87))
        screen.blit(small.render("R = new session, ESC = quit", True, (170, 170, 170)), (x, 107))
        return
    if not out.get("done"):
        screen.blit(mid.render("Jev is thinking...", True, (240, 200, 90)), (x, 54))
        return
    info = f"{out['mode']} | {out['ms']} ms"
    if out.get("conf") is not None:
        info += f" | confidence {out['conf'] * 100:.0f}%"
    screen.blit(small.render(info, True, (150, 200, 150) if out["mode"] == "Jev" else (230, 120, 120)), (x, 54))
    if out.get("err"):
        screen.blit(small.render("API error: " + out["err"][:40], True, (230, 120, 120)), (x, 74))
    probs = out["probs"] or {out["choice"]: 1.0}
    by_id = {o["id"]: o for o in options}
    pct = percents(probs)
    ranked = [(k, p) for k, p in sorted(probs.items(), key=lambda kv: -kv[1]) if pct[k] > 0]
    shown, hidden = ranked[:10], ranked[10:]
    y = 135
    screen.blit(mid.render("Options and probability", True, (200, 200, 200)), (x, y - 5))
    y += 28
    for oid, p in shown:
        o = by_id.get(oid)
        if not o:
            continue
        chosen = oid == out["choice"]
        col = (255, 215, 90) if chosen else (160, 160, 170)
        pygame.draw.rect(screen, (50, 50, 60), (x, y + 20, 300, 10))
        pygame.draw.rect(screen, col, (x, y + 20, int(300 * p), 10))
        label = f"{'>' if chosen else ' '} col {o['col']} rot {o['rot']}  lines {o['lines']} holes {o['holes']}"
        screen.blit(small.render(label, True, col), (x, y))
        screen.blit(small.render(f"{pct[oid]}%", True, col), (x + 310, y + 12))
        y += 42
    rest = len(options) - len(shown)
    if rest > 0:
        other = sum(pct[k] for k, _ in hidden)
        screen.blit(small.render(f"+ {rest} more options, {other}% together", True, (130, 130, 140)), (x, y))


# ---------- main ----------
def new_state(total=0):
    # "total" = pieces placed in this session across games; it survives the auto-restart
    # after game over, so MAX_PIECES limits the whole session, not just one game.
    return {"board": [[0] * W for _ in range(H)], "bag": [], "pieces": 0, "lines": 0,
            "total": total, "next": None, "phase": "spawn", "out": {}, "options": [], "name": None,
            "target": None, "y": 0.0, "t": 0.0, "rot": 0, "col": 3}


def next_piece(st):
    if not st["bag"]:
        st["bag"] = list(SHAPES)
        random.shuffle(st["bag"])
    return st["bag"].pop()


def fall(st, dy, limit):
    """Move the piece down by dy, never past `limit` and never into the stack."""
    cells = rotations(st["name"])[st["rot"]]
    ny = max(st["y"], min(st["y"] + dy, limit))
    if fits(st["board"], cells, st["col"], math.ceil(ny)):
        st["y"] = ny


def steer_step(st):
    """One rotate-or-shift step toward the target placement."""
    tg = st["target"]
    rots = rotations(st["name"])
    n = len(rots)
    rot, col = st["rot"], st["col"]
    if rot != tg["rot"]:
        rot = (rot + (1 if (tg["rot"] - rot) % n <= n // 2 else -1)) % n
    elif col != tg["col"]:
        col += 1 if tg["col"] > col else -1
    if fits(st["board"], rots[rot], col, math.ceil(st["y"])):
        st["rot"], st["col"] = rot, col
    else:  # blocked by the stack: jump to the target, its column is free above it
        st["rot"], st["col"] = tg["rot"], tg["col"]
        st["y"] = min(st["y"], tg["y"])


def main():
    pygame.init()
    screen = pygame.display.set_mode((WIN_W, WIN_H))
    pygame.display.set_caption("Jev plays Tetris")
    fonts = (pygame.font.SysFont("consolas", 26, bold=True),
             pygame.font.SysFont("consolas", 18, bold=True),
             pygame.font.SysFont("consolas", 15))
    clock = pygame.time.Clock()
    st = new_state()
    running = True
    while running:
        dt = clock.tick(30) / 1000
        for e in pygame.event.get():
            if e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE):
                running = False
            if e.type == pygame.KEYDOWN and e.key == pygame.K_r:
                st = new_state()

        if st["phase"] == "spawn" and st["total"] >= MAX_PIECES:
            st["phase"], st["name"], st["target"] = "limit", None, None
        elif st["phase"] == "spawn":
            st["name"] = st["next"] or next_piece(st)
            st["next"] = next_piece(st)
            st["options"] = get_placements(st["board"], st["name"])
            st["out"] = {}
            st["target"], st["rot"], st["col"], st["y"] = None, 0, 3, 0.0
            if not st["options"]:
                st["phase"], st["t"] = "over", 0.0
            else:
                st["phase"] = "think"
                threading.Thread(target=ask_jev, daemon=True,
                                 args=(st["options"], st["name"], st["next"], st["board"], st["out"])).start()
        elif st["phase"] == "think":
            if st["out"].get("done"):
                st["target"] = next(o for o in st["options"] if o["id"] == st["out"]["choice"])
                st["phase"], st["t"] = "steer", 0.0
            else:
                fall(st, THINK_SPEED * dt, THINK_MAX_Y)
        elif st["phase"] == "steer":
            tg = st["target"]
            fall(st, STEER_SPEED * dt, tg["y"])
            st["t"] += dt
            if st["t"] >= STEP_TIME:
                st["t"] = 0.0
                steer_step(st)
            if st["rot"] == tg["rot"] and st["col"] == tg["col"]:
                st["y"] = min(st["y"], tg["y"])
                st["phase"] = "drop"
        elif st["phase"] == "drop":
            st["y"] += DROP_SPEED * dt
            if st["y"] >= st["target"]["y"]:
                st["board"], cleared = apply_piece(st["board"], st["target"]["final"], st["name"])
                st["lines"] += cleared
                st["pieces"] += 1
                st["total"] += 1
                st["phase"] = "spawn"
        elif st["phase"] == "over":
            st["t"] += dt
            if st["t"] > 3:
                st = new_state(st["total"])

        screen.fill((24, 24, 30))
        pygame.draw.rect(screen, (40, 40, 50), (OX, OY, W * CELL, H * CELL))
        for y in range(H):
            for x in range(W):
                if st["board"][y][x]:
                    draw_cell(screen, x, y, COLORS[st["board"][y][x]])
        if st["phase"] in ("think", "steer", "drop") and st["name"]:
            col = COLORS[st["name"]]
            if st["target"] and st["phase"] in ("steer", "drop"):
                for x, y in st["target"]["final"]:
                    draw_cell(screen, x, y, (255, 255, 255), outline=True)
            for x, y in rotations(st["name"])[st["rot"]]:
                draw_cell(screen, x + st["col"], y + st["y"], col)
        if st["phase"] == "over":
            screen.blit(fonts[0].render("GAME OVER", True, (240, 90, 90)), (OX + 60, OY + 250))
        if st["phase"] == "limit":
            screen.blit(fonts[0].render("LIMIT REACHED", True, (240, 200, 90)), (OX + 40, OY + 250))
        draw_panel(screen, fonts, st, st["options"])
        pygame.display.flip()
    pygame.quit()


if __name__ == "__main__":
    main()