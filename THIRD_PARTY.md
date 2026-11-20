# Компоненты BlockHarbor

BlockHarbor 2.5.1 для Windows x64. Имена файлов, адреса загрузки и контрольные суммы SHA-256 зафиксированы в `tools/windows-lock.json`. Исходники оболочки `BlockHarbor.exe` находятся в `tools/bootstrap_windows.go` и `tools/bootstrap_payload.go`, компонента удаления данных — в `tools/data_cleanup`. Сборка приложения и установщика из исходников выполняется командами ниже.

```powershell
python tools/build_windows.py --output ..\BlockHarbor-Windows-x64
python tools/build_installer.py --launcher ..\BlockHarbor-Windows-x64\BlockHarbor.exe --output ..\BlockHarbor-Setup-2.5.1.exe --version 2.5.1
```

Для сборки приложения и компонента удаления нужны Python 3.10 или новее, компилятор Go 1.25 или новее и интернет для первой загрузки. Для установщика нужен NSIS 3.13 или новее. Поставляемая оболочка и компонент удаления собраны с Go 1.27.4. Каталог назначения должен быть новым или пустым. Команда сохраняет проверенные архивы во временном каталоге пользователя и использует их при повторной сборке. Результат сборки — один `BlockHarbor.exe` со встроенным ZIP, Python и приложением. При первом запуске пакет проверяется по SHA-256 и распаковывается в пользовательский каталог. Файлы лицензий и исходники доступны внутри распакованной версии в `%LOCALAPPDATA%\BlockHarbor\app`.

| Компонент | Версия | Назначение и лицензия |
| --- | --- | --- |
| CPython x64 | 3.13.17 | Интерпретатор и стандартная библиотека. Условия Python Software Foundation и включённых компонентов в `runtime/LICENSE.txt`. |
| Go | 1.27.4 | Стандартная библиотека внутри оболочки и компонента удаления данных. BSD-3-Clause, файлы в `runtime/go-notices/`. |
| NSIS | 3.13 | Установщик и встроенные плагины. Оригинальные уведомления и тексты лицензий находятся в `bundled/installer/NSIS-LICENSE.txt`. |
| minecraft-launcher-lib | 8.0 | Подготовка Minecraft Java, BSD-2-Clause. |
| Pillow | 13.0.0 | Обработка изображений скинов. MIT-CMU и лицензии включённых библиотек. |
| Requests | 2.34.2 | HTTP-клиент, Apache-2.0. |
| urllib3 | 2.8.0 | HTTP-соединения, MIT. |
| charset-normalizer | 3.5.2 | Определение кодировок, MIT. |
| idna | 3.20 | Имена доменов, BSD-3-Clause. |
| certifi | 2026.10.23 | Сертификаты центров доверия, MPL-2.0. |

Лицензии Python-пакетов сохранены в каждом каталоге `runtime/Lib/site-packages/*.dist-info/licenses/`. Исходники CPython доступны на [python.org](https://www.python.org/downloads/release/python-31317/), Go на [go.dev](https://go.dev/dl/), пакетов в соответствующих релизах на [PyPI](https://pypi.org/). Minecraft и клиент Steam устанавливаются отдельно.

При выборе «Microsoft через Prism» BlockHarbor получает официальный переносимый Prism Launcher 11.1.1 для Windows x64 MinGW и проверяет его SHA-256. Архив загружается отдельно. Условия лицензии и исходный код опубликованы в [репозитории Prism Launcher](https://github.com/PrismLauncher/PrismLauncher/releases/tag/11.1.1). Microsoft-вход в этом режиме обслуживает Prism. Для локальной игры используется собственный запуск BlockHarbor.

## Игра через Steam

Раздел «Друзья» использует **e4steam 0.3.6** (Kamilchik) и **e4steam Public Servers 0.1.0** (K2 Studio Development). В архив входят оригинальные JAR Public Servers из релиза автора и Fabric API для версии 1.20.1. Файлы лицензий находятся в `bundled/e4steam/licenses`, оригинальные уведомления внутри архивов сохранены. Для прочих подходящих версий BlockHarbor выбирает совместимые файлы по каталогу `launcher/e4steam_catalog.json`.

BlockHarbor при первом запуске сетевой сборки автоматически получает ядро e4steam из официального релиза. JAR содержит нативные библиотеки Steamworks от Valve и уведомления поставщика. После проверки контрольной суммы BlockHarbor кэширует файлы в каталоге пользователя. Лаунчер и эти моды используют клиент Steam, установленный пользователем. Пароли Steam вводятся только в Steam.

- [e4steam 0.3.6](https://github.com/Kamilhik/e4steam/releases/tag/v0.3.6)
- [Лицензии включённых в e4steam компонентов](https://github.com/Kamilhik/e4steam/blob/v0.3.6/THIRD_PARTY_NOTICES.md)
- [e4steam Public Servers 0.1.0](https://github.com/K2-Studio-Development/e4steam-Public-Servers/releases/tag/v0.1.0)
- [Fabric API](https://github.com/FabricMC/fabric)

## Скины персонажа

Лаунчер получает CustomSkinLoader по HTTPS из публикации проекта на Modrinth при первой установке в сборку. Поддерживаемые сочетания версий Minecraft и загрузчиков, точные имена файлов и SHA-512 записаны в `launcher/skin_releases.json`. Для сборок Fabric и Forge 1.20.1 используется соответствующая версия CustomSkinLoader 14.27. [CustomSkinLoader](https://modrinth.com/mod/customskinloader)

Для обмена скинами в общем мире BlockHarbor включает оригинальные JAR мода **Skinss+** автора MineMark. Хозяин мира или выделенный сервер и клиенты участников используют совместимый выпуск. В комплекте есть отдельные JAR для Fabric и Forge на Minecraft 1.18.2, 1.19.2, 1.19.4, 1.20.1, 1.20.2 и 1.21.8. Имена файлов, размер, ссылки на релизы и SHA-512 записаны в `launcher/skin_sync_catalog.json`. Уведомление об авторских правах и текст лицензии MIT находятся в `bundled/skin-sync/LICENSE.txt`. [Skinss+ на Modrinth](https://modrinth.com/mod/skinss-plus)

## Каталоги Minecraft и загрузчиков

`launcher/minecraft_versions.json` и `launcher/minecraft_releases.json` содержат снимок официального каталога версий Mojang. `launcher/loader_catalog.json` хранит сведения о поддерживаемых версиях и, для Forge и NeoForge, опубликованных выпусках загрузчика. Это метаданные для выбора компонентов; исполняемые файлы игры и загрузчиков скачиваются отдельно при подготовке сборки. Лаунчер обновляет каталоги по запросу формы создания сборки с учётом локального кэша.

| Каталог | Источник |
| --- | --- |
| Minecraft Java Edition | [Mojang version manifest v2](https://piston-meta.mojang.com/mc/game/version_manifest_v2.json) |
| Fabric | [Fabric Meta: версии игры](https://meta.fabricmc.net/v2/versions/game) |
| Quilt | [Quilt Meta: версии игры](https://meta.quiltmc.org/v3/versions/game) |
| Forge | [Официальные Maven metadata](https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml) |
| Исторические выпуски Forge | [Индекс версий Prism Launcher](https://meta.prismlauncher.org/v1/net.minecraftforge/index.json) |
| NeoForge | [Официальные Maven metadata](https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml) |

## Каталог дополнений

Основной поиск использует публичный API v2 и CDN сообщества. Лаунчер сверяет размер и SHA-512 каждого файла с метаданными выпуска. Условия распространения задаёт автор проекта. [Документация API](https://docs.modrinth.com/api/)

Дополнительный источник использует выпуски в официальных репозиториях авторов на GitHub. В `launcher/author_releases.json` перечислены репозитории, совместимые версии, прямые ссылки на опубликованные файлы, их размеры и SHA-512. Сами дополнения загружаются при установке. При обновлении списка сохраняются сведения о том, к какому проекту относится файл, включая проекты, опубликованные на обеих площадках.

Среди авторских проектов есть мод Lithium, шейдер Bliss и ресурс-пак Faithful 32x. [Lithium](https://github.com/CaffeineMC/lithium/releases) · [Bliss](https://github.com/X0nk/Bliss-Shader/releases) · [Faithful 32x](https://github.com/Faithful-Resource-Pack/Faithful-32x-Java/releases)
