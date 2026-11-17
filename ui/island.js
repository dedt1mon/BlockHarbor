// Original pixel panorama, drawn locally at logical pixel resolution.
window.drawHarbor = function (canvas) {
  if (!canvas) return;
  const W = 800,
    H = 225;
  canvas.width = W;
  canvas.height = H;
  const g = canvas.getContext("2d");
  g.imageSmoothingEnabled = false;
  let seed = 11419;
  const rnd = () =>
    (seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 4294967296;
  function rect(x, y, w, h, c) {
    g.fillStyle = c;
    g.fillRect(Math.round(x), Math.round(y), Math.ceil(w), Math.ceil(h));
  }
  function poly(pts, c) {
    g.fillStyle = c;
    g.beginPath();
    pts.forEach(([x, y], i) =>
      i
        ? g.lineTo(Math.round(x), Math.round(y))
        : g.moveTo(Math.round(x), Math.round(y)),
    );
    g.closePath();
    g.fill();
  }
  const sky = g.createLinearGradient(0, 0, 0, H);
  sky.addColorStop(0, "#87999f");
  sky.addColorStop(0.36, "#e1bda2");
  sky.addColorStop(0.63, "#e5c098");
  sky.addColorStop(1, "#839d8a");
  g.fillStyle = sky;
  g.fillRect(0, 0, W, H);
  [
    [645, 37, 29, "#ead2b0"],
    [645, 37, 22, "#ffe4ae"],
  ].forEach(([x, y, r, c]) => {
    for (let yy = -r; yy < r; yy += 2) {
      const dx = Math.floor(Math.sqrt(r * r - yy * yy) / 2) * 2;
      rect(x - dx, y + yy, dx * 2, 2, c);
    }
  });
  for (let i = 0; i < 36; i++) {
    let x = rnd() * W,
      y = rnd() * 74,
      w = 12 + rnd() * 42;
    rect(x, y, w, 2, "#f0cfad");
    rect(x + w * 0.2, y - 2, w * 0.52, 2, "#f0cfad");
  }
  function ridge(base, amp, col, step) {
    let pts = [[0, H]];
    for (let x = 0; x <= W + step; x += step) {
      const y =
        base +
        Math.sin(x / 77 + 0.9) * amp * 0.38 +
        Math.sin(x / 32) * amp * 0.29 +
        (rnd() - 0.5) * amp * 0.47;
      pts.push([x, y], [x + step, y]);
    }
    pts.push([W, H]);
    poly(pts, col);
  }
  ridge(96, 32, "#afaaa0", 6);
  ridge(117, 35, "#919c91", 5);
  ridge(136, 22, "#738d81", 4);
  const water = g.createLinearGradient(0, 135, 0, H);
  water.addColorStop(0, "#91aaa0");
  water.addColorStop(1, "#537e7a");
  g.fillStyle = water;
  g.fillRect(0, 139, W, H - 139);
  for (let i = 0; i < 420; i++) {
    const y = 140 + rnd() * 90,
      x = rnd() * W;
    rect(x, y, 1 + rnd() * 18, 1, rnd() > 0.52 ? "#aac1a8" : "#739d94");
  }
  const cube = (x, y, w, h, d, colors) => {
    poly(
      [
        [x, y],
        [x + w, y - w * 0.45],
        [x + w + d, y - w * 0.45 + d * 0.45],
        [x + d, y + d * 0.45],
      ],
      colors[0],
    );
    poly(
      [
        [x, y],
        [x + d, y + d * 0.45],
        [x + d, y + d * 0.45 + h],
        [x, y + h],
      ],
      colors[1],
    );
    poly(
      [
        [x + d, y + d * 0.45],
        [x + w + d, y - w * 0.45 + d * 0.45],
        [x + w + d, y - w * 0.45 + d * 0.45 + h],
        [x + d, y + d * 0.45 + h],
      ],
      colors[2],
    );
  };
  const cx = 574,
    cy = 133,
    b = 10;
  const blocks = [];
  for (let a = -8; a <= 9; a++)
    for (let j = -7; j <= 8; j++) {
      const v = (a * a) / 78 + (j * j) / 48 + Math.sin(a * 1.7 + j) * 0.13;
      if (v > 1) continue;
      const level = Math.max(
        0,
        Math.round((1 - v) * 3 + Math.sin(a * 0.6 + j * 0.5) * 0.65),
      );
      blocks.push({
        a,
        j,
        lev: level,
        x: cx + (a - j) * b,
        y: cy + (a + j) * b * 0.45 - level * 5,
      });
    }
  blocks.sort((k, l) => k.a + k.j - l.a - l.j);
  const trees = [];
  for (const t of blocks) {
    const { a, j, x, y, lev } = t;
    const cliff = 5 + Math.floor(rnd() * 11);
    cube(x - b, y, b, cliff + 3, b, ["#84836a", "#76694f", "#695b47"]);
    cube(x - b, y, b, 3, b, [
      ["#9ba767", "#8fa263", "#aab174", "#879b62"][Math.floor(rnd() * 4)],
      "#6d8451",
      "#647949",
    ]);
    if (rnd() > 0.86 && lev > 0) {
      rect(x - 2, y + 2, 3, 1, "#d4c27e");
      rect(x - 1, y, 1, 2, "#c7b76e");
    }
    if (j <= 2 && (a < 1 || a > 4) && lev > 0 && rnd() < 0.155) trees.push(t);
  }
  // A small spruce grove, built from the same isometric blocks as the shoreline.
  for (const { x, y, a, j } of trees) {
    const n = 2 + Math.floor(rnd() * 2),
      m = 0.72 + rnd() * 0.45;
    cube(x - 1, y - 5, 2, 6, 2, ["#85734b", "#685238", "#584431"]);
    let base = y - 6;
    for (let k = 0; k < n + 1; k++) {
      const r = (n + 2 - k) * 1.8 * m;
      cube(x - r, base - k * 6 * m, r, 4 * m, r, [
        k % 2 ? "#658359" : "#5c7b53",
        "#335e4a",
        "#294f40",
      ]);
    }
  }
  // A wooden landing at the edge of the island.
  for (let k = 0; k < 6; k++) {
    cube(616 + k * 3, 154 + k * 1.35, 2.8, 2, 7, [
      "#ad9066",
      "#7a6b53",
      "#685c48",
    ]);
  }
  // Foreground shore and shadowed pines frame the view.
  function pine(x, y, s) {
    rect(x - 1.8 * s, y - 6 * s, 3 * s, 12 * s, "#314c3f");
    for (let k = 0; k < 4; k++) {
      const yy = y - 9 * s - k * 6 * s,
        r = (10 - k * 2) * s;
      poly(
        [
          [x, yy - 13 * s],
          [x + r, yy + 3 * s],
          [x + r * 0.6, yy + 3 * s],
          [x + r * 0.6, yy + 5 * s],
          [x - r, yy + 5 * s],
        ],
        k % 2 ? "#345845" : "#2c503f",
      );
    }
  }
  poly(
    [
      [686, 225],
      [728, 207],
      [748, 208],
      [777, 186],
      [800, 179],
      [800, 225],
    ],
    "#436753",
  );
  pine(772, 199, 1.4);
  pine(799, 192, 2);
  pine(748, 208, 0.8);
};
