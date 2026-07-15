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
const feedbackBlock = document.getElementById("feedback");
const feedbackBtn = document.getElementById("feedback-btn");
const feedbackForm = document.getElementById("feedback-form");
const feedbackComment = document.getElementById("feedback-comment");
const feedbackSend = document.getElementById("feedback-send");
const feedbackDone = document.getElementById("feedback-done");

let stream = null;
let currentLogId = null;

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

// Редирект с закрытого раздела (?forbidden=1) - показываем уведомление вместо
// «сырого» 403, пользователь остаётся в портале.
if (new URLSearchParams(location.search).has("forbidden")) {
  document.getElementById("forbidden").classList.remove("hidden");
  history.replaceState(null, "", "/");
}

// Пункты меню по роли (роль узнаём с сервера; экраны всё равно закрыты 403,
// меню лишь не показывает лишнее). Админам - Документы и Журнал; super-admin -
// плюс Пользователи и Коллекции.
fetch("/api/me")
  .then((response) => response.json())
  .then((me) => {
    const admin = me.role === "collection_admin" || me.role === "super_admin";
    if (admin) {
      document.getElementById("nav-documents").classList.remove("hidden");
      document.getElementById("nav-logs").classList.remove("hidden");
    }
    if (me.role === "super_admin") {
      document.getElementById("nav-users").classList.remove("hidden");
      document.getElementById("nav-collections").classList.remove("hidden");
    }
  });

const recentBlock = document.getElementById("recent");
const recentList = document.getElementById("recent-list");

// Три последних вопроса пользователя - обновляем на загрузке и после нового ответа.
function loadRecent() {
  fetch("/api/history/recent")
    .then((response) => response.json())
    .then((items) => {
      recentList.replaceChildren();
      for (const item of items) {
        const link = document.createElement("a");
        link.href = `/history/${item.id}`;
        link.textContent = item.question;
        const when = new Date(item.created_at).toLocaleString("ru-RU");
        const meta = document.createElement("span");
        meta.className = "history-meta";
        meta.textContent = ` — ${when}${item.refused ? " · отказ" : ""}`;
        const row = document.createElement("li");
        row.append(link, meta);
        recentList.append(row);
      }
      recentBlock.classList.toggle("hidden", items.length === 0);
    });
}

loadRecent();

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

  resetFeedback(data.log_id);
  loadRecent();  // новый вопрос попал в лог - обновим блок последних
}

function resetFeedback(logId) {
  currentLogId = logId ?? null;
  feedbackForm.classList.add("hidden");
  feedbackDone.classList.add("hidden");
  feedbackComment.value = "";
  // Отмечать можно, только если запись лога создалась (есть её id).
  feedbackBtn.classList.toggle("hidden", currentLogId === null);
  feedbackBlock.classList.toggle("hidden", currentLogId === null);
}

feedbackBtn.addEventListener("click", () => {
  feedbackBtn.classList.add("hidden");
  feedbackForm.classList.remove("hidden");
  feedbackComment.focus();
});

feedbackSend.addEventListener("click", () => {
  if (currentLogId === null) return;
  feedbackSend.disabled = true;
  fetch("/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ log_id: currentLogId, comment: feedbackComment.value.trim() || null }),
  })
    .then((response) => {
      feedbackSend.disabled = false;
      feedbackForm.classList.add("hidden");
      feedbackDone.classList.remove("hidden");
    })
    .catch(() => {
      feedbackSend.disabled = false;
    });
});

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
