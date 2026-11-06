/* Market Lens aurora: the logo's colours (blue, violet, teal) bloom softly at random places behind the Console,
   with a few faint sparkles. Pure CSS animation (transform and opacity only); this script only picks new random
   places, sizes, colours and timings each time a bloom ends. Off for people who ask for reduced motion. */
(function () {
  var host = document.getElementById("aurora");
  if (!host) return;
  var still = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var COLORS = ["--au-1", "--au-2", "--au-3", "--au-4", "--au-5"];
  function rnd(a, b) { return a + Math.random() * (b - a); }
  function pick(list) { return list[Math.floor(Math.random() * list.length)]; }

  function bloom(el, first) {
    var s = rnd(34, 62);                        /* size in vmax */
    el.style.setProperty("--x", rnd(-15, 95).toFixed(1) + "vw");
    el.style.setProperty("--y", rnd(-20, 90).toFixed(1) + "vh");
    el.style.setProperty("--s", s.toFixed(1) + "vmax");
    el.style.setProperty("--dx", rnd(-14, 14).toFixed(1) + "vw");
    el.style.setProperty("--dy", rnd(-10, 10).toFixed(1) + "vh");
    el.style.setProperty("--c", "var(" + pick(COLORS) + ")");
    if (first) {  /* timing is set once (changing it later would make a bloom jump); start mid-bloom, not all at once */
      el.style.animationDuration = rnd(16, 28).toFixed(1) + "s";
      el.style.animationDelay = (-rnd(0, 14)).toFixed(1) + "s";
    }
  }
  function sparkle(el, first) {
    el.style.left = rnd(2, 98).toFixed(2) + "vw";
    el.style.top = rnd(2, 98).toFixed(2) + "vh";
    el.style.setProperty("--z", rnd(.5, 1.25).toFixed(2));
    if (first) {
      el.style.animationDuration = rnd(3.5, 7.5).toFixed(1) + "s";
      el.style.animationDelay = (-rnd(0, 7)).toFixed(1) + "s";
    }
  }
  /* each loop ends fully faded out, so the next one simply starts somewhere new */
  function restart(el, fn) { fn(el, false); }

  var frag = document.createDocumentFragment();
  for (var i = 0; i < 6; i++) {
    var b = document.createElement("i");
    b.className = "au-blob";
    bloom(b, true);
    if (!still) b.addEventListener("animationiteration", function () { restart(this, bloom); });
    frag.appendChild(b);
  }
  var n = window.innerWidth < 760 ? 10 : 22;
  for (var j = 0; j < n; j++) {
    var p = document.createElement("i");
    p.className = "au-spark";
    sparkle(p, true);
    if (!still) p.addEventListener("animationiteration", function () { restart(this, sparkle); });
    frag.appendChild(p);
  }
  host.appendChild(frag);

  /* the aurora pauses while the tab is hidden, so it never costs anything in the background */
  document.addEventListener("visibilitychange", function () {
    host.classList.toggle("paused", document.hidden);
  });
})();
