(() => {
  const data = document.getElementById("account-data");
  if (!data) return;

  const accounts = JSON.parse(data.textContent);
  const provider = document.getElementById("provider");
  const account = document.getElementById("account");
  const accountOptions = document.getElementById("accounts");
  const accountSection = document.getElementById("account-section");
  const accountKey = document.getElementById("account-key");
  const statement = document.getElementById("statement");
  const fileTypes = document.getElementById("file-types");
  const selectedFile = document.getElementById("selected-file");
  const submit = document.getElementById("submit");
  const dropzone = document.getElementById("dropzone");
  const note = document.getElementById("provider-note");

  const normalise = (value) => value.trim().toLowerCase();
  const choicesFor = () => accounts.filter((item) => item.provider === normalise(provider.value));

  function resetAccount() {
    account.value = "";
    accountKey.value = "";
    statement.value = "";
    statement.accept = "";
    selectedFile.textContent = "";
    submit.disabled = true;
    fileTypes.textContent = "Selecciona primero una entidad y una cuenta.";
  }

  function selectAccount() {
    const choice = choicesFor().find((item) => item.account === normalise(account.value));
    accountKey.value = choice ? choice.key : "";
    statement.accept = choice ? choice.accept : "";
    fileTypes.textContent = choice
      ? `Formatos permitidos: ${choice.extensions.join(", ")}`
      : "Elige una cuenta de la lista.";
    submit.disabled = !choice || !statement.files.length;
  }

  provider.addEventListener("input", () => {
    const choices = choicesFor();
    resetAccount();
    accountOptions.replaceChildren(...choices.map((choice) => {
      const option = document.createElement("option");
      option.value = choice.account;
      return option;
    }));
    accountSection.hidden = !choices.length;
    note.textContent = normalise(provider.value) === "fonditel"
      ? "Fonditel aún no tiene un conversor disponible."
      : choices.length ? "Busca o elige la cuenta que corresponde al extracto." : "Elige una entidad de la lista.";
    if (choices.length === 1) {
      account.value = choices[0].account;
      selectAccount();
    }
  });
  account.addEventListener("input", selectAccount);
  statement.addEventListener("change", () => {
    selectedFile.textContent = statement.files[0] ? statement.files[0].name : "";
    submit.disabled = !accountKey.value || !statement.files.length;
  });
  ["dragenter", "dragover"].forEach((event) => dropzone.addEventListener(event, (e) => {
    e.preventDefault();
    dropzone.classList.add("dragging");
  }));
  ["dragleave", "drop"].forEach((event) => dropzone.addEventListener(event, (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragging");
  }));
  dropzone.addEventListener("drop", (event) => {
    if (!event.dataTransfer.files.length) return;
    statement.files = event.dataTransfer.files;
    statement.dispatchEvent(new Event("change"));
  });
})();
