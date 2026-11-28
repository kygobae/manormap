// Geoportal WMTS tiles that exist only in the Polish grid (EPSG:2180, PUWG 1992) on a normal Web Mercator Leaflet map.
// Each map tile is filled from the 2180 tiles under it with one affine transform (rotation + scale): over a tile's few
// hundred metres the two projections differ by far less than a pixel. Used for the LiDAR relief, whose WMS takes
// 10-25 s per tile while its WMTS answers in ~0.3 s. Needs window.L; works from file:// (images are only drawn).
(function () {
  "use strict";
  // EPSG:2180 = Transverse Mercator on GRS80, central meridian 19°, k0 0.9993, false easting 500 km, northing -5300 km.
  const a = 6378137, f = 1 / 298.257222101, e2 = f * (2 - f), ep2 = e2 / (1 - e2), k0 = 0.9993, lon0 = 19 * Math.PI / 180;
  const e4 = e2 * e2, e6 = e4 * e2;
  const m1 = 1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256, m2 = 3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024,
        m3 = 15 * e4 / 256 + 45 * e6 / 1024, m4 = 35 * e6 / 3072;
  function pl92(lat, lon) {                                   // degrees -> [easting, northing] in metres (Snyder 8-9, 8-10)
    const p = lat * Math.PI / 180, s = Math.sin(p), c = Math.cos(p), t = Math.tan(p);
    const N = a / Math.sqrt(1 - e2 * s * s), T = t * t, C = ep2 * c * c, A = (lon * Math.PI / 180 - lon0) * c;
    const M = a * (m1 * p - m2 * Math.sin(2 * p) + m3 * Math.sin(4 * p) - m4 * Math.sin(6 * p));
    const A2 = A * A;
    const x = k0 * N * (A + (1 - T + C) * A * A2 / 6 + (5 - 18 * T + T * T + 72 * C - 58 * ep2) * A * A2 * A2 / 120);
    const y = k0 * (M + N * t * (A2 / 2 + (5 - T + 9 * C + 4 * C * C) * A2 * A2 / 24
                                 + (61 - 58 * T + T * T + 600 * C - 330 * ep2) * A2 * A2 * A2 / 720));
    return [x + 500000, y - 5300000];
  }

  // Geoportal's EPSG:2180 tile matrix set: top-left N 850 000 / E 100 000, 512 px tiles, scale denominators per level
  const SCALES = [7559538.928571429, 3779769.4642857146, 1889884.7321428573, 944942.3660714286, 472471.1830357143,
    236235.59151785716, 94494.23660714286, 47247.11830357143, 23623.559151785714, 9449.423660714287,
    4724.711830357143, 1889.8847321428573, 944.9423660714286];
  const TOP = 850000, LEFT = 100000, TILE = 512;

  const cache = new Map();                                    // url -> Promise<img|null>, shared by neighbouring tiles
  function load(url) {
    let p = cache.get(url);
    if (p) { cache.delete(url); cache.set(url, p); return p; } // keep recently used ones last
    p = new Promise(res => { const im = new Image(); im.onload = () => res(im); im.onerror = () => res(null); im.src = url; });
    cache.set(url, p);
    if (cache.size > 300) cache.delete(cache.keys().next().value);
    return p;
  }

  window.pl2180Layer = function (url, opts) {                 // url with {z} {x} {y} = matrix level, column, row
    const Layer = window.L.GridLayer.extend({
      createTile(coords, done) {
        const size = this.getTileSize(), r = Math.min(window.devicePixelRatio || 1, 2);
        const tile = document.createElement("canvas");
        tile.width = size.x * r; tile.height = size.y * r;
        const nw = coords.scaleBy(size), map = this._map;
        const ll = (dx, dy) => map.unproject([nw.x + dx, nw.y + dy], coords.z);
        const P = [[0, 0], [size.x, 0], [0, size.y], [size.x, size.y]].map(([dx, dy]) => { const q = ll(dx, dy); return pl92(q.lat, q.lng); });
        // tile pixel -> metres: P0 + px/size.x * u + py/size.y * v; invert to metres -> pixel
        const u = [P[1][0] - P[0][0], P[1][1] - P[0][1]], v = [P[2][0] - P[0][0], P[2][1] - P[0][1]];
        const j11 = u[0] / size.x, j21 = u[1] / size.x, j12 = v[0] / size.y, j22 = v[1] / size.y, det = j11 * j22 - j12 * j21;
        const i11 = j22 / det, i12 = -j12 / det, i21 = -j21 / det, i22 = j11 / det;
        // the finest level that is not sharper than needed (fewer, larger images), capped at the finest level there is
        const need = Math.hypot(u[0], u[1]) / size.x;
        let z = 0;
        while (z < SCALES.length - 1 && SCALES[z + 1] * 0.00028 >= need * 0.75) z++;
        const res = SCALES[z] * 0.00028, span = res * TILE;
        const xs = P.map(p => p[0]), ys = P.map(p => p[1]);
        const c0 = Math.floor((Math.min(...xs) - LEFT) / span), c1 = Math.floor((Math.max(...xs) - LEFT) / span);
        const r0 = Math.floor((TOP - Math.max(...ys)) / span), r1 = Math.floor((TOP - Math.min(...ys)) / span);
        const jobs = [];
        for (let row = Math.max(r0, 0); row <= r1; row++)
          for (let col = Math.max(c0, 0); col <= c1; col++)
            jobs.push(load(window.L.Util.template(url, {z, x: col, y: row})).then(im => ({im, row, col})));
        Promise.all(jobs).then(list => {
          const ctx = tile.getContext("2d");
          ctx.imageSmoothingQuality = "high";
          for (const {im, row, col} of list) {
            if (!im) continue;
            const dE = LEFT + col * span - P[0][0], dN = TOP - row * span - P[0][1];
            // image pixel (i, j) -> metres (E0 + i*res, N0 - j*res) -> tile pixel, times the device pixel ratio
            ctx.setTransform(r * i11 * res, r * i21 * res, -r * i12 * res, -r * i22 * res,
                             r * (i11 * dE + i12 * dN), r * (i21 * dE + i22 * dN));
            ctx.drawImage(im, -0.5, -0.5, TILE + 1, TILE + 1);  // half a pixel of overlap hides seams between images
          }
          done(null, tile);
        });
        return tile;
      }
    });
    return new Layer(Object.assign({maxZoom: 20}, opts));
  };
  window.pl2180Layer.pl92 = pl92;                             // for tests
})();
