const form = document.getElementById("ask-form");
const questionInput = document.getElementById("question");
const submitButton = document.getElementById("submit");
const resetButton = document.getElementById("reset");
const statusBlock = document.getElementById("status");
const resultBlock = document.getElementById("result");
const answerBlock = document.getElementById("answer");
const sourcesBlock = document.getElementById("sources-block");
const sourcesList = document.getElementById("sources");
const metaBlock = document.getElementById("meta");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = questionInput.value.trim();
  if (!question) return;

  submitButton.disabled = true;
  resultBlock.classList.add("hidden");
  statusBlock.classList.remove("hidden", "error");
  statusBlock.textContent = "Думаю...";

  try {
    const response = await fetch("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || "Ошибка сервера. Попробуйте ещё раз.");
    }

    render(await response.json());
  } catch (error) {
    statusBlock.classList.add("error");
    statusBlock.textContent = error.message;
  } finally {
    submitButton.disabled = false;
  }
});

// «Новый вопрос»: очистить поле и прошлый ответ, вернуть фокус в поле ввода.
resetButton.addEventListener("click", () => {
  questionInput.value = "";
  resultBlock.classList.add("hidden");
  statusBlock.classList.add("hidden");
  resetButton.classList.add("hidden");
  questionInput.focus();
});

function render(data) {
  statusBlock.classList.add("hidden");
  resultBlock.classList.remove("hidden");
  resultBlock.classList.toggle("refused", data.refused);
  resetButton.classList.remove("hidden");
  answerBlock.textContent = data.answer;

  sourcesList.replaceChildren();
  sourcesBlock.classList.toggle("hidden", data.sources.length === 0);
  for (const source of data.sources) {
    const link = document.createElement("a");
    link.href = `/manuals/${source.source_path}`;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = source.pages
      ? `${source.title} (${source.pages})`
      : source.title;

    const item = document.createElement("li");
    item.append(link);
    sourcesList.append(item);
  }

  metaBlock.textContent =
    `сходство: ${data.best_similarity} | время: ${data.elapsed_seconds} с`;
}
