// Popup script — Sign-in form + signed-in status.

async function checkAuth() {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: "get_auth" }, (r) => resolve((r && r.token) || ""));
  });
}

function showSignedIn(text) {
  document.getElementById("signed-out").style.display = "none";
  document.getElementById("signed-in").style.display = "";
  if (text) document.getElementById("status").textContent = text;
}

function showSignedOut() {
  document.getElementById("signed-out").style.display = "";
  document.getElementById("signed-in").style.display = "none";
}

function setError(msg) {
  const el = document.getElementById("err");
  el.textContent = msg || "";
  el.classList.toggle("show", !!msg);
}

document.addEventListener("DOMContentLoaded", async () => {
  const token = await checkAuth();
  if (token) showSignedIn("Signed in.");
  else showSignedOut();
});

document.getElementById("sign-in").addEventListener("click", async () => {
  const btn = document.getElementById("sign-in");
  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value;
  if (!username || !password) {
    setError("Enter your email and password.");
    return;
  }
  setError("");
  btn.disabled = true;
  btn.textContent = "Signing in…";
  const r = await new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: "login", username, password }, resolve);
  });
  btn.disabled = false;
  btn.textContent = "Sign in";
  if (r && r.ok) {
    showSignedIn("Signed in as " + (r.user_email || username));
  } else {
    setError((r && r.error) || "Sign-in failed.");
  }
});

document.getElementById("sign-out").addEventListener("click", async () => {
  await new Promise((resolve) => chrome.runtime.sendMessage({ type: "logout" }, resolve));
  showSignedOut();
});
