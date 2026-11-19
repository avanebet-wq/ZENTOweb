// Самый первый скрипт: помечает страницу как работающую с JavaScript (нужен для появления блоков).
// Вынесен из HTML, потому что сервер запрещает инлайн-скрипты (CSP: script-src 'self').
document.documentElement.classList.add('js');
