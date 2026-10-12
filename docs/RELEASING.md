# Выпуск релиза

Версия мода хранится в `mod.json` (`version`, целое число). Она попадает в `.modinfo` и в имя архива.

## Перед релизом

1. `python tools/civ6loc.py status` — все нужные моды `ready`.
2. `python tools/civ6loc.py validate` — 0 ошибок и 0 предупреждений.
3. `python tools/civ6loc.py install`, проверка в игре по [`TESTING.md`](TESTING.md).
4. Увеличьте `version` в `mod.json`, выполните `python tools/civ6loc.py sync-docs`, закоммитьте.

## GitHub Releases

```powershell
python tools/civ6loc.py package
```

В `dist/release/` появятся `RuLocalization_LeadersAndCivs-v<версия>.zip` и файл с контрольной суммой. Затем:

```powershell
git tag v<версия>
git push origin v<версия>
gh release create v<версия> dist/release/* --title "v<версия>" --notes "Что изменилось"
```

## Steam Workshop

Загрузка идёт через [steamcmd](https://developer.valvesoftware.com/wiki/SteamCMD) от аккаунта, на котором есть Civilization VI.

1. Один раз положите картинку `workshop/preview.png` (PNG/JPG, лучше 512x512, до 1 МБ). Текст описания лежит в `workshop/description.bbcode`, список модов подставляется автоматически.
2. Подготовьте файлы:

   ```powershell
   python tools/civ6loc.py workshop --note "Что изменилось"
   ```

3. Выполните команду, которую выведет скрипт:

   ```powershell
   steamcmd +login <логин> +workshop_build_item "<путь>\dist\workshop\workshop_item.vdf" +quit
   ```

4. При первой публикации steamcmd напечатает `PublishFileID`. Впишите его в `workshop/workshop.json` (`publishedfileid`): все следующие загрузки обновят тот же мод, а не создадут новый.

По умолчанию `visibility` равен `2` (скрытый). Проверьте страницу мода в мастерской и поменяйте на `0` (публичный) для открытия или смените видимость на странице самого мода. Значения: 0 публичный, 1 для друзей, 2 скрытый, 3 по ссылке.

Ссылку на мастерскую после первой публикации добавьте в `README.md` (раздел «Установка»).
