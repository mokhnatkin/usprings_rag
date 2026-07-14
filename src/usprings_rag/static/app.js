const form = document.getElementById("ask-form");
const questionInput = document.getElementById("question");
const collectionSelect = document.getElementById("collection");
const submitButton = document.getElementById("submit");
const resetButton = document.getElementById("reset");
const statusBlock = document.getElementById("status");
const resultBlock = document.getElementById("result");
const answerBlock = document.getElementById("answer");
const sourcesBlock = document.getElementById("sources-block");
const sourcesList = document.getElementById("sources");
const metaBlock = document.getElementById("meta");

let stream = null;

// Список коллекций - с сервера: справочник один и тот же для поиска и для UI.
// Первая в списке (1С:ERP) выбрана по умолчанию - основная база пилота.
fetch("/collections")
  .then((response) => response.json())
  .then((collections) => {
    for (const collection of collections) {
      const option = document.createElement("option");
      option.value = collection.code;
      option.textContent = collection.title;
      collectionSelect.append(option);
    }
  });

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const question = questionInput.value.trim();
  if (!question) return;

  submitButton.disabled = true;
  resetButton.classList.add("hidden");
  resultBlock.classList.add("hidden");
  resultBlock.classList.remove("refused");
  answerBlock.textContent = "";
  sourcesBlock.classList.add("hidden");
  sourcesList.replaceChildren();
  metaBlock.textContent = "";
  statusBlock.classList.remove("hidden", "error");
  statusBlock.textContent = "Думаю...";

  const collection = collectionSelect.value;
  stream = new EventSource(
    `/ask/stream?question=${encodeURIComponent(question)}` +
      `&collection=${encodeURIComponent(collection)}`
  );

  stream.addEventListener("message", (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "delta") appendDelta(data.text);
    else if (data.type === "done") finish(data);
    else if (data.type === "error") fail(data.message);
  });

  // Сеть/сервер отвалились до события done - иначе индикатор «Думаю...» завис бы.
  stream.addEventListener("error", () => {
    if (stream) fail("Не удалось получить ответ. Попробуйте ещё раз.");
  });
});

resetButton.addEventListener("click", () => {
  questionInput.value = "";
  resultBlock.classList.add("hidden");
  statusBlock.classList.add("hidden");
  resetButton.classList.add("hidden");
  questionInput.focus();
});

function appendDelta(text) {
  // Первая дельта: убираем «Думаю...» и показываем область ответа.
  statusBlock.classList.add("hidden");
  resultBlock.classList.remove("hidden");
  answerBlock.textContent += text;
}

function finish(data) {
  closeStream();
  statusBlock.classList.add("hidden");
  resultBlock.classList.remove("hidden");
  resultBlock.classList.toggle("refused", data.refused);
  resetButton.classList.remove("hidden");
  submitButton.disabled = false;

  // Отказ приходит целиком в done (LLM не вызывалась либо ответа в контексте нет).
  if (data.answer) answerBlock.textContent = data.answer;

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

  // База знаний в meta: видно, по какой коллекции получен ответ или отказ.
  metaBlock.textContent =
    `база: ${data.collection} | сходство: ${data.best_similarity} ` +
    `| время: ${data.elapsed_seconds} с`;
}

function fail(message) {
  closeStream();
  resultBlock.classList.add("hidden");
  statusBlock.classList.remove("hidden");
  statusBlock.classList.add("error");
  statusBlock.textContent = message;
  submitButton.disabled = false;
}

function closeStream() {
  if (stream) {
    stream.close();
    stream = null;
  }
}
