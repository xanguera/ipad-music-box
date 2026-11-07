/*
 * The kids' player. Written in ES5 for iOS 9 Safari: no let/const, no arrow functions,
 * no fetch, no Promises, no NodeList.forEach, no Element.closest.
 *
 * Two jobs:
 * 1. Browsing. Links load only the changing part of the page (an HTML fragment) into #browse,
 *    so this page, and the <video> in it, never reloads and the music keeps playing.
 * 2. Playing. A queue of songs plays one after another in the same <video> element.
 *    The Mac downloads a song the first time it is played, so the player may wait a few seconds.
 *    When the queue runs out, the Mac suggests related songs and the music goes on.
 */
(function () {
  "use strict";

  var browse = document.getElementById("browse");
  var video = document.getElementById("video");
  var els = {
    title: document.getElementById("now-title"),
    sub: document.getElementById("now-sub"),
    play: document.getElementById("b-play"),
    seek: document.getElementById("seek"),
    cur: document.getElementById("t-cur"),
    dur: document.getElementById("t-dur"),
    qList: document.getElementById("queue-list"),
    qCount: document.getElementById("q-count"),
    fsPlay: document.getElementById("fs-play"),
    fsSeek: document.getElementById("fs-seek"),
    fsCur: document.getElementById("fs-cur"),
    fsDur: document.getElementById("fs-dur"),
    loves: [document.getElementById("b-love"), document.getElementById("fs-love")]
  };
  var SILENCE = "/static/silence.mp4";
  // Songs come as HLS. Safari (iPad, Mac) plays it itself. Other browsers get hls.js, loaded only
  // for them: Chrome says it can play HLS too, but its player fails on a song still being prepared.
  var ua = navigator.userAgent;
  var IS_SAFARI = /Safari/.test(ua) && !/Chrome|Chromium|CriOS|Edg|OPR|Firefox|FxiOS/.test(ua);
  var NATIVE_HLS = !!video.canPlayType("application/vnd.apple.mpegurl") && (IS_SAFARI || !window.MediaSource);
  var hls = null;
  var STORE_KEY = "musicbox.queue";

  // ---------------------------------------------------------------- helpers

  function matches(el, sel) {
    var fn = el.matches || el.webkitMatchesSelector || el.msMatchesSelector;
    return fn ? fn.call(el, sel) : false;
  }

  function closest(el, sel) {
    while (el && el.nodeType === 1) {
      if (matches(el, sel)) { return el; }
      el = el.parentNode;
    }
    return null;
  }

  function request(method, url, headers, done) {
    var xhr = new XMLHttpRequest();
    xhr.open(method, url, true);
    for (var h in headers) { if (headers.hasOwnProperty(h)) { xhr.setRequestHeader(h, headers[h]); } }
    xhr.onreadystatechange = function () {
      if (xhr.readyState === 4) { done(xhr.status, xhr.responseText); }
    };
    xhr.send(null);
  }

  function getJSON(method, url, done) {
    request(method, url, {}, function (status, text) {
      var data = null;
      try { data = JSON.parse(text); } catch (e) { data = null; }
      done(status, data);
    });
  }

  function fmt(sec) {
    if (!isFinite(sec) || sec < 0) { sec = 0; }
    sec = Math.floor(sec);
    var m = Math.floor(sec / 60), s = sec % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  var toastTimer = null;
  function toast(text) {
    var t = document.getElementById("toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "toast";
      document.body.appendChild(t);
    }
    t.textContent = text;
    t.style.display = "block";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.style.display = "none"; }, 2200);
  }

  function store(key, value) {
    try { window.localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* private mode */ }
  }

  function load(key) {
    try { return JSON.parse(window.localStorage.getItem(key)); } catch (e) { return null; }
  }

  // --------------------------------------------------------------- browsing

  var navToken = 0;

  // done(ok), if given, hears whether the page arrived.
  function navigate(url, push, done) {
    var token = ++navToken;
    browse.style.opacity = "0.5";
    request("GET", url, { "X-Fragment": "1" }, function (status, html) {
      if (token !== navToken) { return; }  // a newer tap won
      browse.style.opacity = "1";
      if (status !== 200) {
        if (done) { done(false); } else { toast("Could not open that page"); }
        return;
      }
      browse.innerHTML = html;
      if (push) { history.pushState({ url: url }, "", url); }
      window.scrollTo(0, 0);
      markPlaying();
      if (done) { done(true); }
    });
  }

  window.addEventListener("popstate", function () {
    navigate(location.pathname + location.search, false);
  });
  history.replaceState({ url: location.pathname + location.search }, "", location.pathname + location.search);

  function songFromRow(row) {
    return {
      id: row.getAttribute("data-id"),
      title: row.getAttribute("data-title"),
      channel: row.getAttribute("data-channel"),
      duration: parseInt(row.getAttribute("data-duration"), 10) || 0
    };
  }

  function songsIn(list) {
    var rows = list ? list.querySelectorAll(".song") : [];
    var out = [];
    for (var i = 0; i < rows.length; i++) { out.push(songFromRow(rows[i])); }
    return out;
  }

  function listFor(el) {
    var section = closest(el, ".section") || browse;
    return section.querySelector("ul.songs");
  }

  function loadMore(link) {
    var row = closest(link, ".more-row");
    link.textContent = "Loading...";
    request("GET", link.getAttribute("href"), { "X-Fragment": "1" }, function (status, html) {
      if (status !== 200) { link.textContent = "More songs"; toast("Could not load more"); return; }
      var tmp = document.createElement("ul");
      tmp.innerHTML = html;
      var parent = row.parentNode;
      while (tmp.firstChild) { parent.insertBefore(tmp.firstChild, row); }
      parent.removeChild(row);
      markPlaying();
    });
  }

  document.addEventListener("click", function (e) {
    var t = e.target;
    var el;

    if ((el = closest(t, ".song .add"))) {
      e.preventDefault();
      enqueue([songFromRow(closest(el, ".song"))]);
      return;
    }
    if ((el = closest(t, ".song .play"))) {
      e.preventDefault();
      var songRow = closest(el, ".song");
      var list = closest(songRow, "ul.songs");
      var all = songsIn(list);
      var id = songRow.getAttribute("data-id");
      var start = 0;
      for (var i = 0; i < all.length; i++) { if (all[i].id === id) { start = i; break; } }
      playList(all, start);
      return;
    }
    if ((el = closest(t, ".playall")) || (el = closest(t, ".shuffle"))) {
      e.preventDefault();
      var songs = songsIn(listFor(el));
      if (!songs.length) { toast("No songs here"); return; }
      if (matches(el, ".shuffle")) { shuffle(songs); }
      playList(songs, 0);
      return;
    }
    if ((el = closest(t, "a.more"))) {
      e.preventDefault();
      loadMore(el);
      return;
    }
    if ((el = closest(t, "a"))) {
      var href = el.getAttribute("href");
      if (!href || href === "#") { e.preventDefault(); return; }
      // Admin pages and other sites open as normal pages.
      if (el.hasAttribute("data-plain") || href.indexOf("/admin") === 0 || /^[a-z]+:/i.test(href)) { return; }
      e.preventDefault();
      closeQueue();
      document.body.className = document.body.className.replace(" big", "");
      navigate(href, true);
    }
  });

  // ---------------------------------------------------------------- playing

  var queue = [];      // [{id, title, channel}]
  var index = -1;      // the song playing now
  var unlocked = false;
  var playToken = 0;   // bumps on every new song, so stale callbacks do nothing
  var countedPlay = false;
  var loadedId = null; // the song whose file is in the <video> now
  var freshStart = false; // a new song was just loaded and should begin at 0

  function shuffle(a) {
    for (var i = a.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1));
      var tmp = a[i]; a[i] = a[j]; a[j] = tmp;
    }
  }

  // iOS only lets a page start sound from inside a tap. We play a tiny silent clip during the tap;
  // after that the same <video> element may start songs on its own (needed for auto-next).
  function unlock() {
    if (unlocked) { return; }
    unlocked = true;
    try {
      video.src = SILENCE;
      video.load();
      video.play();
    } catch (e) { /* ignore */ }
  }

  function playList(songs, start) {
    unlock();
    queue = songs.slice();
    playAt(start);
  }

  function enqueue(songs) {
    for (var i = 0; i < songs.length; i++) { queue.push(songs[i]); }
    if (index < 0) {
      unlock();
      playAt(queue.length - songs.length);
    } else {
      toast("Added to the queue");
      saveQueue();
      renderQueue();
      prefetchNext();
    }
  }

  function playAt(i) {
    if (i < 0 || i >= queue.length) { return; }
    index = i;
    var song = queue[i];
    var token = ++playToken;
    countedPlay = false;
    // Stop the old song at once, so a skip is heard even while the new song downloads.
    if (loadedId && loadedId !== song.id) { video.pause(); }
    showNow(song, "Getting it ready...");
    showLove(song.id);
    saveQueue();
    renderQueue();
    markPlaying();
    prepare(song.id, token, 0);
  }

  function prepare(id, token, tries) {
    getJSON("POST", "/api/prepare/" + id, function (status, data) {
      if (token !== playToken) { return; }
      if (status === 404) { failed("This song is not in the music box any more"); return; }
      if (status !== 200 || !data) {
        if (tries < 3) { setTimeout(function () { prepare(id, token, tries + 1); }, 2000); }
        else { failed("The music box is not answering"); }
        return;
      }
      waitReady(id, token, data.status, 0);
    });
  }

  function waitReady(id, token, status, polls) {
    if (token !== playToken) { return; }
    // "streaming": the Mac is still writing the song, but its first chunks are there to play.
    if (status === "ready" || status === "streaming") { start(id, token); return; }
    if (status.indexOf("error") === 0) { failed("This song does not work right now"); return; }
    if (polls > 180) { failed("This song is taking too long"); return; }  // about 3 minutes
    // "missing": the Mac dropped the song (a skip cancelled it just as it was asked for again,
    // or the admin removed its file). Ask for it again instead of waiting for nothing.
    var ask = status === "missing" ? ["POST", "/api/prepare/" + id] : ["GET", "/api/status/" + id];
    els.sub.textContent = status === "downloading" ? "Downloading..."
      : status === "converting" ? "Getting it ready for the iPad..." : "Waiting for its turn...";
    setTimeout(function () {
      getJSON(ask[0], ask[1], function (code, data) {
        waitReady(id, token, code === 200 && data ? data.status : "queued", polls + 1);
      });
    }, 1000);
  }

  function start(id, token) {
    if (token !== playToken) { return; }
    var url = "/media/" + id + "/index.m3u8";
    loadedId = id;
    freshStart = true;
    showNow(queue[index], queue[index].channel);
    if (NATIVE_HLS) {
      video.src = url;
      video.load();
      playNow();
      return;
    }
    withHlsJs(function () {
      if (token !== playToken) { return; }
      if (!hls) {
        hls = new window.Hls({ startPosition: 0 });  // from the start, even while the song still grows
        hls.attachMedia(video);
      }
      hls.loadSource(url);
      playNow();
    });
  }

  function playNow() {
    var p = video.play();
    if (p && p["catch"]) { p["catch"](function () { showPaused(); }); }  // newer browsers return a promise
  }

  var hlsWaiting = null;
  function withHlsJs(done) {
    if (window.Hls) { done(); return; }
    if (hlsWaiting) { hlsWaiting.push(done); return; }
    hlsWaiting = [done];
    var tag = document.createElement("script");
    tag.src = "/static/vendor/hls.light.min.js";
    tag.onload = function () {
      var list = hlsWaiting;
      hlsWaiting = null;
      for (var i = 0; i < list.length; i++) { list[i](); }
    };
    tag.onerror = function () { hlsWaiting = null; failed("This browser cannot play the music"); };
    document.head.appendChild(tag);
  }

  function failed(message) {
    var token = playToken;
    els.sub.textContent = message;
    toast(message + " - skipping");
    setTimeout(function () { if (token === playToken) { next(); } }, 2500);
  }

  function next() {
    if (index < queue.length - 1) { playAt(index + 1); return; }
    video.pause();
    showPaused();
    if (index >= 0) { playRelated(); } else { toast("That was the last song"); }
  }

  // The queue ran out: ask the Mac for songs related to the last one and carry on.
  var relatedToken = 0;
  function playRelated() {
    var token = ++relatedToken;
    var last = queue[index];
    var seen = [];
    for (var i = Math.max(0, queue.length - 60); i < queue.length; i++) { seen.push(queue[i].id); }
    els.sub.textContent = "Finding more songs like this...";
    getJSON("GET", "/api/related/" + last.id + "?not=" + seen.join(","), function (status, data) {
      if (token !== relatedToken || queue[index] !== last || index < queue.length - 1) { return; }  // someone picked a song
      var songs = status === 200 && data && data.songs ? data.songs : [];
      if (!songs.length) { els.sub.textContent = ""; toast("That was the last song"); return; }
      for (var j = 0; j < songs.length; j++) { queue.push(songs[j]); }
      toast("More songs like this one");
      playAt(index + 1);
    });
  }

  function prev() {
    var current = queue[index];
    // Early in a song, or while it is still loading, go back a song; later on, restart it.
    if (current && loadedId === current.id && video.currentTime > 4) { video.currentTime = 0; return; }
    if (index > 0) { playAt(index - 1); } else if (current && loadedId === current.id) { video.currentTime = 0; }
  }

  function prefetchNext() {
    var n = queue[index + 1];
    if (n) { getJSON("POST", "/api/prepare/" + n.id + "?prefetch=1", function () {}); }
  }

  function isSilence() {
    return (video.currentSrc || video.src || "").indexOf(SILENCE) >= 0;
  }

  // While a song is still being prepared the player does not know its full length yet,
  // so use the length from the library when there is one.
  function songLength() {
    var song = queue[index];
    if (song && song.duration) { return song.duration; }
    return isFinite(video.duration) ? video.duration : 0;
  }

  // Debug log (Settings): tell the server what the video element is doing, so a stall shows in its log.
  var DEBUG = document.body.getAttribute("data-debug") === "1";
  function report(what) {
    if (!DEBUG || isSilence() || index < 0) { return; }
    var end = video.buffered.length ? video.buffered.end(video.buffered.length - 1) : 0;
    var err = video.error ? video.error.code : "";
    request("POST", "/api/log?e=" + what + "&id=" + queue[index].id + "&t=" + video.currentTime.toFixed(1) +
      "&buf=" + end.toFixed(1) + "&rs=" + video.readyState + "&ns=" + video.networkState + "&err=" + err +
      "&paused=" + video.paused, {}, function () {});
  }
  var reported = ["waiting", "stalled", "error", "playing", "ended", "emptied", "abort"];
  for (var r = 0; r < reported.length; r++) {
    video.addEventListener(reported[r], (function (name) { return function () { report(name); }; })(reported[r]));
  }
  var lastTime = -1;
  setInterval(function () {  // playing, yet the position has not moved for 5 seconds
    if (!video.paused && !video.ended && video.currentTime === lastTime) { report("stuck"); }
    lastTime = video.currentTime;
  }, 5000);

  video.addEventListener("playing", function () {
    if (isSilence()) { return; }
    if (freshStart) {
      freshStart = false;
      // A player may treat a song still being prepared as live and jump to its newest part.
      if (video.currentTime > 5) { video.currentTime = 0; }
    }
    showPlaying();
    if (!countedPlay && index >= 0) {
      countedPlay = true;
      getJSON("POST", "/api/played/" + queue[index].id, function () {});
      prefetchNext();
    }
  });
  video.addEventListener("pause", function () { if (!isSilence()) { showPaused(); } });
  video.addEventListener("ended", function () { if (!isSilence()) { next(); } });
  video.addEventListener("error", function () {
    if (isSilence() || index < 0 || !video.src) { return; }
    failed("This song would not play");
  });
  // Two time bars, the player bar's and full screen's, showing the same thing.
  var bars = [
    { seek: els.seek, cur: els.cur, dur: els.dur },
    { seek: els.fsSeek, cur: els.fsCur, dur: els.fsDur }
  ];
  var seeking = false;

  video.addEventListener("timeupdate", function () {
    if (isSilence()) { return; }
    var d = songLength();
    for (var i = 0; i < bars.length; i++) {
      bars[i].cur.textContent = fmt(video.currentTime);
      bars[i].dur.textContent = fmt(d);
      if (!seeking && d > 0) { bars[i].seek.value = Math.min(100, Math.round(video.currentTime / d * 1000) / 10); }
    }
  });

  function seekFrom(bar) {
    var d = songLength();
    var target = d * bar.value / 100;
    // Only the part already prepared can be reached.
    var s = video.seekable;
    if (s && s.length) { target = Math.min(target, s.end(s.length - 1) - 1); }
    if (d > 0 && target >= 0) { video.currentTime = target; }
    seeking = false;
  }
  for (var b = 0; b < bars.length; b++) {
    (function (bar) {
      bar.addEventListener("touchstart", function () { seeking = true; showFsControlsIfBig(); });
      bar.addEventListener("mousedown", function () { seeking = true; showFsControlsIfBig(); });
      bar.addEventListener("input", function () { showFsControlsIfBig(); });  // keep them up while dragging
      bar.addEventListener("change", function () { seekFrom(bar); showFsControlsIfBig(); });
    })(bars[b].seek);
  }

  function showNow(song, sub) {
    els.title.textContent = song ? song.title : "Pick a song";
    els.sub.textContent = sub || "";
    for (var i = 0; i < bars.length; i++) {
      bars[i].seek.value = 0;
      bars[i].cur.textContent = "0:00";
      bars[i].dur.textContent = "0:00";
    }
  }
  // Drawn icons, not text symbols: those look different (and often clumsy) on every device.
  var PLAY_ICON = '<svg class="i-play" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4.5v15l12.5-7.5z"/></svg>';
  var PAUSE_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 5h4v14h-4zM13.5 5h4v14h-4z"/></svg>';
  function showPlaying() { setPlayButtons(PAUSE_ICON, "Pause"); }
  function showPaused() { setPlayButtons(PLAY_ICON, "Play"); }
  function setPlayButtons(html, label) {
    var buttons = [els.play, els.fsPlay];
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].innerHTML = html;
      buttons[i].setAttribute("aria-label", label);
    }
  }

  function togglePlay() {
    if (index < 0) {
      // Nothing chosen yet: play the first list on the page.
      var songs = songsIn(browse.querySelector("ul.songs"));
      if (songs.length) { playList(songs, 0); } else { toast("Pick a song first"); }
      return;
    }
    if (!video.src || isSilence()) { unlock(); playAt(index); return; }  // e.g. queue restored after a reload
    if (video.paused) { video.play(); } else { video.pause(); }
  }
  function onTap(id, fn) { document.getElementById(id).addEventListener("click", fn); }
  onTap("b-play", togglePlay);
  onTap("b-next", function () { unlock(); next(); });
  onTap("b-prev", function () { unlock(); prev(); });

  // ------------------------------------------------------------ full screen

  // Only the video. A tap shows the heart and close (top right), the time bar and the play buttons.
  var fsTimer = null;
  function hasClass(name) { return (" " + document.body.className + " ").indexOf(" " + name + " ") >= 0; }
  function removeClass(name) { document.body.className = (" " + document.body.className + " ").replace(" " + name + " ", " ").replace(/^\s+|\s+$/g, ""); }
  function addClass(name) { if (!hasClass(name)) { document.body.className += " " + name; } }

  function showFsControls() {
    addClass("fs-show");
    clearTimeout(fsTimer);
    fsTimer = setTimeout(function () { removeClass("fs-show"); }, 4000);
  }
  function leaveFullScreen() { removeClass("big"); removeClass("fs-show"); clearTimeout(fsTimer); }
  function showFsControlsIfBig() { if (hasClass("big")) { showFsControls(); } }

  // Pinching in with two fingers closes full screen. iOS Safari reports pinches itself
  // (gesturechange with a scale); other browsers get the distance between two touches.
  var PINCH_CLOSE = 0.75;  // fingers end at three quarters of where they started, or closer
  var pinchStart = 0;
  function touchGap(e) {
    var a = e.touches[0], b = e.touches[1];
    return Math.sqrt(Math.pow(a.clientX - b.clientX, 2) + Math.pow(a.clientY - b.clientY, 2));
  }
  var screenEl = document.getElementById("screen");
  screenEl.addEventListener("gesturechange", function (e) {
    e.preventDefault();
    if (hasClass("big") && e.scale < PINCH_CLOSE) { leaveFullScreen(); }
  });
  screenEl.addEventListener("touchstart", function (e) {
    pinchStart = e.touches.length === 2 ? touchGap(e) : 0;
  });
  screenEl.addEventListener("touchmove", function (e) {
    if (!pinchStart || e.touches.length !== 2) { return; }
    e.preventDefault();
    if (hasClass("big") && touchGap(e) < pinchStart * PINCH_CLOSE) { pinchStart = 0; leaveFullScreen(); }
  });

  onTap("b-big", function () { closeQueue(); addClass("big"); });
  onTap("fs-tap", function () {
    if (!hasClass("big")) {  // a tap on the small video makes it full screen
      if (index >= 0) { closeQueue(); addClass("big"); }
      return;
    }
    if (hasClass("fs-show")) { removeClass("fs-show"); } else { showFsControls(); }
  });
  onTap("big-close", leaveFullScreen);
  onTap("fs-play", function () { togglePlay(); showFsControls(); });
  onTap("fs-next", function () { unlock(); next(); showFsControls(); });
  onTap("fs-prev", function () { unlock(); prev(); showFsControls(); });

  // ------------------------------------------------------------------ loved

  var loveToken = 0;
  function setLove(loved) {
    for (var i = 0; i < els.loves.length; i++) {
      els.loves[i].innerHTML = loved ? "&#9829;" : "&#9825;";
      els.loves[i].className = loved ? "love-btn on" : "love-btn";
    }
  }
  function showLove(id) {
    var token = ++loveToken;
    setLove(false);
    getJSON("GET", "/api/love/" + id, function (status, data) {
      if (token === loveToken && status === 200 && data) { setLove(data.loved); }
    });
  }
  function toggleLove() {
    if (index < 0) { toast("Pick a song first"); return; }
    var token = ++loveToken;
    getJSON("POST", "/api/love/" + queue[index].id, function (status, data) {
      if (token !== loveToken || status !== 200 || !data) { return; }
      setLove(data.loved);
      toast(data.loved ? "Added to Loved" : "Taken out of Loved");
    });
  }
  onTap("b-love", toggleLove);
  onTap("fs-love", function () { toggleLove(); showFsControls(); });

  // ------------------------------------------------------------------ admin

  // The admin pages open inside the player page, so the music keeps playing. The PIN comes first,
  // on a number pad: no keyboard, which iOS 9 does not handle well over a page like this one.
  var pin = "";
  function showPin() {
    var dots = "";
    for (var i = 0; i < pin.length; i++) { dots += "\u25CF "; }
    document.getElementById("pin-dots").textContent = dots || "\u00A0";
  }
  function openPinPad() { pin = ""; showPin(); addClass("show-pin"); }
  function closePinPad() { pin = ""; removeClass("show-pin"); }

  var adminTimer = null;
  function openAdmin() {
    closeQueue();
    leaveFullScreen();
    var box = document.getElementById("admin-scroll");
    box.innerHTML = '<iframe src="/admin/" title="Admin" scrolling="no"></iframe>';
    var frame = box.firstChild;
    // The frame grows to its page's height, so the whole player page scrolls, not a box inside it.
    function fit() {
      try {
        var doc = frame.contentWindow.document;
        frame.style.height = Math.max(doc.documentElement.scrollHeight, doc.body.scrollHeight, 400) + "px";
      } catch (e) { /* not loaded yet */ }
    }
    frame.addEventListener("load", function () { fit(); window.scrollTo(0, 0); });
    clearInterval(adminTimer);
    adminTimer = setInterval(fit, 700);
    addClass("show-admin");
    window.scrollTo(0, 0);
  }

  onTap("b-admin", function () {
    getJSON("GET", "/admin/session", function (status, data) {
      if (status === 200 && data && data.admin) { openAdmin(); } else { openPinPad(); }
    });
  });
  document.getElementById("pin-pad").addEventListener("click", function (e) {
    var key = closest(e.target, "[data-key]");
    if (!key) { return; }
    var k = key.getAttribute("data-key");
    if (k === "del") { pin = pin.slice(0, -1); showPin(); return; }
    if (k !== "ok") { if (pin.length < 12) { pin += k; } showPin(); return; }
    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/admin/login?json=1", true);
    xhr.setRequestHeader("Content-Type", "application/x-www-form-urlencoded");
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) { return; }
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (err) { data = null; }
      if (data && data.ok) { closePinPad(); openAdmin(); return; }
      pin = "";
      showPin();
      toast(data ? "Wrong PIN" : "The music box is not answering");
    };
    xhr.send("pin=" + encodeURIComponent(pin));
  });
  onTap("pin-cancel", closePinPad);
  // Back to the player: the admin pages call this from their "Player" link.
  function closeAdmin() {
    clearInterval(adminTimer);
    removeClass("show-admin");
    document.getElementById("admin-scroll").innerHTML = "";
    navigate(location.pathname + location.search, false);  // show what the admin changed
  }
  window.musicboxCloseAdmin = closeAdmin;

  function markPlaying() {
    var rows = browse.querySelectorAll(".song");
    var id = index >= 0 && queue[index] ? queue[index].id : null;
    for (var i = 0; i < rows.length; i++) {
      var on = rows[i].getAttribute("data-id") === id;
      rows[i].className = on ? "song playing" : "song";
    }
    refreshTicks();
  }

  // A green tick on "+" means the song is waiting in the queue. Clearing the queue clears them.
  function refreshTicks() {
    var waiting = {};
    for (var i = index + 1; i < queue.length; i++) { waiting[queue[i].id] = true; }
    var buttons = browse.querySelectorAll(".song .add");
    for (var j = 0; j < buttons.length; j++) {
      var on = waiting[closest(buttons[j], ".song").getAttribute("data-id")] === true;
      buttons[j].className = on ? "add done" : "add";
      buttons[j].textContent = on ? "\u2713" : "+";
    }
  }

  // ------------------------------------------------------------------ queue

  function saveQueue() { store(STORE_KEY, { queue: queue, index: index }); }

  function renderQueue() {
    var html = [];
    for (var i = 0; i < queue.length; i++) {
      html.push('<li' + (i === index ? ' class="current"' : '') + '><a class="qj" href="#" data-i="' + i + '"><span>'
        + escapeHTML(queue[i].title) + '</span></a><button class="qx" type="button" data-i="' + i + '">&times;</button></li>');
    }
    els.qList.innerHTML = html.join("");
    var left = queue.length - index - 1;
    els.qCount.textContent = left > 0 ? String(left) : "";
    refreshTicks();
  }

  function escapeHTML(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function closeQueue() { document.body.className = document.body.className.replace(" show-queue", ""); }

  document.getElementById("b-queue").addEventListener("click", function () {
    if (document.body.className.indexOf(" show-queue") >= 0) { closeQueue(); }
    else { document.body.className += " show-queue"; renderQueue(); }
  });
  document.getElementById("queue-close").addEventListener("click", closeQueue);
  document.getElementById("queue-clear").addEventListener("click", function () {
    // Keep the song playing now; drop everything else.
    queue = index >= 0 ? [queue[index]] : [];
    index = queue.length ? 0 : -1;
    saveQueue();
    renderQueue();
  });
  els.qList.addEventListener("click", function (e) {
    var jump = closest(e.target, ".qj");
    var drop = closest(e.target, ".qx");
    if (jump) {
      e.preventDefault();
      e.stopPropagation();
      unlock();
      playAt(parseInt(jump.getAttribute("data-i"), 10));
    } else if (drop) {
      e.stopPropagation();
      var i = parseInt(drop.getAttribute("data-i"), 10);
      if (i === index) { queue.splice(i, 1); index -= 1; next(); }
      else { queue.splice(i, 1); if (i < index) { index -= 1; } }
      saveQueue();
      renderQueue();
    }
  });

  // Restore the queue after a reload. Nothing starts until someone taps play (iOS requires that anyway).
  var saved = load(STORE_KEY);
  if (saved && saved.queue && saved.queue.length) {
    queue = saved.queue;
    index = Math.min(Math.max(saved.index, 0), queue.length - 1);
    showNow(queue[index], "Tap play to continue");
    showLove(queue[index].id);
    renderQueue();
    markPlaying();
  }
})();
