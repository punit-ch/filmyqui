/* FilmyQui front-end polish. Everything here is optional: the site works without it.
   No libraries, no inline code (the Content-Security-Policy forbids it), nothing is sent anywhere. */
(() => {
    "use strict";

    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const finePointer = window.matchMedia("(hover: hover) and (pointer: fine)").matches;
    const YOUTUBE_KEY = /^[A-Za-z0-9_-]{6,20}$/;

    // 1. Trailer: load the YouTube player only when someone clicks play (keeps the page fast).
    document.querySelectorAll("[data-trailer]").forEach((link) => {
        link.addEventListener("click", (event) => {
            const key = link.dataset.trailer || "";
            if (!YOUTUBE_KEY.test(key)) return;  // never build a URL from anything unexpected
            event.preventDefault();
            const frame = document.createElement("iframe");
            frame.src = `https://www.youtube-nocookie.com/embed/${encodeURIComponent(key)}?autoplay=1&rel=0`;
            frame.title = "Movie trailer";
            frame.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
            frame.referrerPolicy = "strict-origin-when-cross-origin";
            frame.allowFullscreen = true;
            link.replaceChildren(frame);
            link.removeAttribute("href");
            link.removeAttribute("aria-label");
        });
    });

    // 2. Sideways scrollers (genre chips): arrow buttons, edge fades, and the chosen chip scrolled into view.
    document.querySelectorAll("[data-scroller]").forEach((box) => {
        const track = box.querySelector(".scroller-track");
        const prev = box.querySelector(".prev");
        const next = box.querySelector(".next");
        if (!track) return;

        const update = () => {
            const max = track.scrollWidth - track.clientWidth;
            box.classList.toggle("can-left", track.scrollLeft > 4);
            box.classList.toggle("can-right", track.scrollLeft < max - 4);
        };
        const nudge = (direction) => {
            track.scrollBy({ left: direction * track.clientWidth * 0.8, behavior: reduceMotion ? "auto" : "smooth" });
        };
        prev && prev.addEventListener("click", () => nudge(-1));
        next && next.addEventListener("click", () => nudge(1));
        track.addEventListener("scroll", update, { passive: true });
        window.addEventListener("resize", update);

        const current = track.querySelector('[aria-current="true"], [aria-current="page"]');
        if (current) {  // set scrollLeft directly so the page itself never jumps
            track.style.scrollBehavior = "auto";
            track.scrollLeft = Math.max(0, current.offsetLeft - (track.clientWidth - current.offsetWidth) / 2);
            track.style.scrollBehavior = "";
        }
        update();
    });

    // 3. Headline words blur into focus.
    if (!reduceMotion) {
        document.querySelectorAll("[data-blur-text]").forEach((el) => {
            const text = el.textContent.trim();
            if (!text) return;
            el.setAttribute("aria-label", text);
            const frag = document.createDocumentFragment();
            text.split(/\s+/).forEach((word, i, all) => {
                const span = document.createElement("span");
                span.className = "blur-word";
                span.setAttribute("aria-hidden", "true");
                span.style.setProperty("--i", String(i));
                span.textContent = word;
                frag.append(span);
                if (i < all.length - 1) frag.append(" ");
            });
            el.replaceChildren(frag);
        });
    }

    // 4. Scroll reveal. Only elements below the fold are hidden, then eased in as they arrive.
    if (!reduceMotion && "IntersectionObserver" in window) {
        const seen = new IntersectionObserver((entries) => {
            entries.forEach((entry) => {
                if (!entry.isIntersecting) return;
                entry.target.classList.add("is-in");
                seen.unobserve(entry.target);
            });
        }, { rootMargin: "0px 0px -6% 0px", threshold: 0.08 });

        let row = 0, lastTop = null;
        document.querySelectorAll("[data-reveal]").forEach((el) => {
            const box = el.getBoundingClientRect();
            if (box.top < window.innerHeight) return;  // already on screen: leave it alone
            row = lastTop !== null && Math.abs(box.top - lastTop) < 8 ? row + 1 : 0;
            lastTop = box.top;
            el.style.setProperty("--d", `${Math.min(row, 6) * 70}ms`);  // stagger across a row
            el.classList.add("reveal-pending");
            seen.observe(el);
        });
    }

    // 5. Poster tilt and cursor spotlight (mouse and trackpad only).
    if (!reduceMotion && finePointer) {
        const MAX_TILT = 9;  // degrees
        document.querySelectorAll(".card").forEach((card) => {
            const frame = card.querySelector(".poster-frame");
            if (!frame) return;
            let waiting = false;
            card.addEventListener("pointermove", (event) => {
                if (waiting) return;
                waiting = true;
                requestAnimationFrame(() => {
                    waiting = false;
                    const box = frame.getBoundingClientRect();
                    const x = (event.clientX - box.left) / box.width;
                    const y = (event.clientY - box.top) / box.height;
                    frame.classList.add("is-tracking");
                    frame.style.setProperty("--mx", `${(x * 100).toFixed(1)}%`);
                    frame.style.setProperty("--my", `${(y * 100).toFixed(1)}%`);
                    frame.style.setProperty("--ry", `${((x - 0.5) * 2 * MAX_TILT).toFixed(2)}deg`);
                    frame.style.setProperty("--rx", `${((0.5 - y) * 2 * MAX_TILT).toFixed(2)}deg`);
                });
            });
            card.addEventListener("pointerleave", () => {
                frame.classList.remove("is-tracking");
                frame.style.removeProperty("--rx");
                frame.style.removeProperty("--ry");
            });
        });
        document.querySelectorAll(".stub").forEach((stub) => {
            stub.addEventListener("pointermove", (event) => {
                const box = stub.getBoundingClientRect();
                stub.style.setProperty("--mx", `${event.clientX - box.left}px`);
                stub.style.setProperty("--my", `${event.clientY - box.top}px`);
            });
        });
    }

    // 6. Show/hide password buttons.
    document.querySelectorAll("[data-pw-toggle]").forEach((button) => {
        const input = document.getElementById(button.dataset.pwToggle);
        if (!input) return;
        button.hidden = false;
        button.addEventListener("click", () => {
            const show = input.type === "password";
            input.type = show ? "text" : "password";
            button.textContent = show ? "Hide" : "Show";
            button.setAttribute("aria-pressed", String(show));
        });
    });
})();
