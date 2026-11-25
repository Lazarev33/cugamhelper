"use strict";
const theme = document.querySelector("#theme");
const saved = localStorage.getItem("cugam-theme");
function selectTheme(value) {
    const current = ["system", "dark", "light"].includes(value) ? value : "system";
    theme.value = current;
    document.documentElement.dataset.theme = current;
}
selectTheme(saved);
theme.addEventListener("change", () => {
    selectTheme(theme.value);
    localStorage.setItem("cugam-theme", theme.value);
});
const menu = document.querySelector(".navtoggle");
menu.addEventListener("click", () => {
    const open = menu.getAttribute("aria-expanded") !== "true";
    menu.setAttribute("aria-expanded", String(open));
    document.querySelector("#nav").dataset.open = String(open);
});
const dialog = document.querySelector("#confirmation");
let pending;
document.addEventListener("submit", (event) => {
    if (!event.target.dataset.confirm) return;
    event.preventDefault();
    pending = event.target;
    dialog.querySelector("p").textContent = pending.dataset.confirm;
    dialog.returnValue = "";
    dialog.showModal();
});
dialog.addEventListener("close", () => {
    if (dialog.returnValue === "accept" && pending) {
        delete pending.dataset.confirm;
        pending.requestSubmit();
    }
    pending = undefined;
});
