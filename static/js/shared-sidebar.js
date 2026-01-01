async function initSidebar() {
    try {
        // Fetch the shared sidebar HTML
        const response = await fetch("/templates/sidebar.html")
        if (!response.ok) throw new Error("Failed to load sidebar")

        const sidebarHTML = await response.text()

        // Insert sidebar at the beginning of the body or into a container with id 'sidebar-container'
        const sidebarContainer = document.getElementById("sidebar-container") || document.body
        const sidebarElement = document.createElement("div")
        sidebarElement.innerHTML = sidebarHTML
        sidebarContainer.insertBefore(sidebarElement.firstElementChild, sidebarContainer.firstChild)

        // Highlight the active nav item based on current page
        highlightActiveNav()
    } catch (error) {
        console.error("[v0] Error loading sidebar:", error)
    }
}

// Highlight active navigation item based on current page URL
function highlightActiveNav() {
    const currentPath = window.location.pathname
    const navItems = document.querySelectorAll("[data-nav]")

    navItems.forEach((item) => {
        const href = item.getAttribute("href")
        const isActive =
            currentPath === href || (currentPath === "/" && href === "/") || (currentPath.startsWith(href) && href !== "/")

        if (isActive) {
            item.classList.add("text-white", "bg-slate-700")
            item.classList.remove("text-gray-400")
        } else {
            item.classList.remove("text-white", "bg-slate-700")
            item.classList.add("text-gray-400")
        }
    })
}

// Initialize sidebar when DOM is ready
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initSidebar)
} else {
    initSidebar()
}
