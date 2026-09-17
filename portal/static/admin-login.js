document.querySelector("#adminLoginForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.target.querySelector("button");
  const error = document.querySelector("#loginError");
  error.classList.add("hidden");
  button.disabled = true;
  button.firstChild.textContent = "Signing in... ";
  const payload = Object.fromEntries(new FormData(event.target));
  try {
    const response = await fetch("/api/auth/admin-login", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || "Sign-in failed.");
    location.href = "/";
  } catch (failure) {
    error.textContent = failure.message;
    error.classList.remove("hidden");
    button.disabled = false;
    button.firstChild.textContent = "Sign in ";
  }
});
