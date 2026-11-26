// Load the YouTube player only when someone clicks play (keeps the movie page fast).
document.querySelectorAll("[data-trailer]").forEach((link) => {
    link.addEventListener("click", (event) => {
        event.preventDefault();
        const frame = document.createElement("iframe");
        frame.src = `https://www.youtube-nocookie.com/embed/${link.dataset.trailer}?autoplay=1&rel=0`;
        frame.title = "Movie trailer";
        frame.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
        frame.allowFullscreen = true;
        link.replaceChildren(frame);
        link.removeAttribute("href");
        link.removeAttribute("aria-label");
    });
});
