# Matiz

Terminal para Linux/GNOME feito em Python com GTK 4, libadwaita e VTE (o mesmo motor de
emulação do GNOME Terminal e do Ptyxis). Foi pensado para quem trabalha com agentes de IA
no terminal: abas coloridas para identificar o que roda em cada uma, sessões que sobrevivem
ao fechar a janela ou reiniciar o computador e um painel com o uso do Claude Code, OpenCode
e Codex.

## Recursos

- **Abas coloridas.** Clique com o botão direito no título da aba (ou use `Ctrl+Alt+1…8`) para
  escolher uma cor. Ela aparece na aba, numa faixa no topo, no fundo do terminal e na barra
  da janela.
- **Sessões persistentes.** Um processo em segundo plano (`matiz-daemon`) é dono das shells:
  - ao **fechar a janela**, tudo continua rodando; ao reabrir, cada aba se reconecta e a tela
    é reexibida;
  - ao **reiniciar o computador**, o Matiz recria as abas a partir do estado salvo: o
    Claude Code volta na mesma conversa (`claude --resume <id>`), programas seguros (ssh, vim,
    htop, `npm run dev`…) são reexecutados e qualquer outro comando aparece digitado no prompt,
    esperando seu Enter. Nada perigoso (`sudo`, `rm`…) é executado sozinho.
- **Painéis.** Divida a aba lado a lado ou em cima/embaixo. Cada painel é uma sessão
  independente e o layout é restaurado.
- **Avisos.** Quando um comando demorado termina ou o Claude Code termina de responder numa aba
  que você não está olhando, a aba fica destacada e, com a janela em segundo plano, aparece uma
  notificação do sistema.
- **Ctrl+clique** em URLs, caminhos e `arquivo:linha:coluna` (inclusive tracebacks do Python):
  abre no navegador, no editor na linha certa ou no gerenciador de arquivos.
- **Widget de uso de IA** na barra superior: sessões abertas, login, mensagens e tokens
  (5 h / hoje / 7 dias), custo quando a ferramenta registra, gráfico das últimas 24 h e
  **percentual dos limites do plano** (5 h / semanal). Tudo é lido de arquivos locais.
- Busca no histórico, salvar histórico em arquivo, abrir a pasta atual em outro aplicativo,
  abas fixadas, menu com todos os atalhos (`Ctrl+?`).

## Requisitos

- Linux com GTK ≥ 4.14 e libadwaita ≥ 1.8 (testado no Ubuntu 26.04 / GNOME 50)
- Python ≥ 3.10 com PyGObject
- VTE para GTK 4 (`Vte 3.91`)

No Debian/Ubuntu:

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-vte-3.91
```

Não há dependências do PyPI.

## Instalação

```bash
git clone <url-do-repositorio> matiz
cd matiz
./bin/matiz
```

Para aparecer no menu de aplicativos, crie `~/.local/share/applications/dev.matiz.Terminal.desktop`
(troque o caminho pelo local onde você clonou):

```ini
[Desktop Entry]
Name=Matiz
Comment=Terminal com abas coloridas e sessão persistente
Exec=/caminho/para/matiz/bin/matiz
Icon=utilities-terminal
Terminal=false
Type=Application
Categories=System;TerminalEmulator;
StartupWMClass=dev.matiz.Terminal
```

Para abrir com `Ctrl+Alt+T` no GNOME, libere o atalho padrão e crie um personalizado:

```bash
K=org.gnome.settings-daemon.plugins.media-keys
P=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/matiz/
gsettings set $K terminal "[]"
gsettings set $K.custom-keybinding:$P name 'Matiz'
gsettings set $K.custom-keybinding:$P command "$PWD/bin/matiz"
gsettings set $K.custom-keybinding:$P binding '<Primary><Alt>t'
gsettings set $K custom-keybindings "['$P']"
```

## Atalhos

Todos também estão no menu ☰, no botão direito do título da aba e no botão direito dentro do
terminal. `Ctrl+?` ou `F1` mostram a lista completa.

| Ação | Atalho |
|---|---|
| Nova aba / nova aba nesta pasta | `Ctrl+Shift+T` / `Ctrl+Shift+D` |
| Fechar painel (ou a aba, se for o único) | `Ctrl+Shift+W` ou o ✕ no canto do painel |
| Dividir lado a lado / em cima e embaixo | `Alt+Shift++` / `Alt+Shift+-` |
| Ir para o painel vizinho | `Alt+←` `↑` `→` `↓` |
| Cor da aba / sem cor | `Ctrl+Alt+1…8` / `Ctrl+Alt+0` |
| Renomear aba | `Ctrl+Shift+R` |
| Copiar / colar | `Ctrl+Shift+C` / `Ctrl+Shift+V` |
| Abrir a pasta no gerenciador de arquivos | `Ctrl+Shift+E` |
| Copiar caminho da pasta | `Ctrl+Shift+Y` |
| Buscar no terminal | `Ctrl+Shift+F` (Enter ↑, Shift+Enter ↓, Esc fecha) |
| Salvar histórico em arquivo | `Ctrl+Shift+S` |
| Limpar tela e histórico | `Ctrl+Shift+K` |
| Próxima / anterior aba | `Ctrl+PgDn` / `Ctrl+PgUp` |
| Mover aba | `Ctrl+Shift+PgDn` / `Ctrl+Shift+PgUp` |
| Ir para a aba N | `Alt+1…9` |
| Zoom | `Ctrl++` / `Ctrl+-` / `Ctrl+0` |
| Abrir link ou arquivo:linha | `Ctrl+clique` |

## Widget de uso de IA

| Ferramenta | De onde vêm os dados |
|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` (tokens por mensagem) e `~/.claude/sessions/` (sessões abertas) |
| OpenCode | `~/.local/share/opencode/opencode.db` (tokens e custo), aberto somente leitura |
| Codex | `~/.codex/sessions/**/*.jsonl` (tokens e percentual dos limites do plano) |

O login é verificado com os comandos oficiais (`claude auth status`, `codex login status`); o
botão **Entrar** abre uma aba com `claude auth login` / `codex login`. O Matiz não lê senhas
nem tokens e não faz requisições de rede.

**Limites do plano do Claude.** O Claude Code entrega o percentual dos limites (5 h, semanal…)
somente ao comando configurado como barra de status (`statusLine`). No widget, o botão
**Ativar** instala `bin/matiz-statusline` como intermediário em `~/.claude/settings.json`:
ele anota os números para o Matiz e repassa a entrada, sem alteração, à barra de status que já
existia. Antes de alterar, é criado um backup em `settings.json.matiz-bak`; **Desativar**
restaura a configuração anterior. Se a pasta do Matiz for removida, o comando cai direto na
barra de status original.

## Configuração

Opcional, em `~/.config/matiz/config.json`:

```json
{
  "reexecutar_extra": [["python", "manage.py", "runserver"], ["docker", "compose", "up"]],
  "persistir_processos": true,
  "linhas_historico_salvas": 3000,
  "linhas_rolagem": 20000,
  "fonte": "JetBrains Mono 11",
  "notificar_apos_segundos": 10,
  "notificacoes": true,
  "editor": ["code", "--goto", "{file}:{line}:{col}"],
  "widget_uso": true
}
```

| Chave | Efeito |
|---|---|
| `reexecutar_extra` | Comandos (prefixos de argv) que podem ser reexecutados sozinhos ao restaurar |
| `persistir_processos` | `false` desliga o daemon: os processos morrem ao fechar a janela |
| `linhas_historico_salvas` | Linhas de histórico gravadas em disco por painel |
| `fonte` | Fonte do terminal (padrão: a monoespaçada do sistema) |
| `notificar_apos_segundos` | Duração mínima de um comando para gerar aviso |
| `editor` | Comando para abrir `arquivo:linha` no Ctrl+clique (padrão: VS Code, Cursor, Zed ou Sublime, o que estiver instalado) |
| `widget_uso` | `false` esconde o widget de uso |

## Como funciona

```
Matiz (janela GTK)  ──socket Unix──▶  matiz-daemon  ──▶  um PTY por painel → shell → programas
```

| Arquivo | Papel |
|---|---|
| `matiz/app.py` | Janela, abas, menus, atalhos, avisos, ciclo salvar/restaurar |
| `matiz/tab.py` | Aba: cor, título e árvore de painéis (`Gtk.Paned`) |
| `matiz/pane.py` | Painel: `Vte.Terminal`, conexão com o daemon, links, detecção do que está rodando |
| `matiz/daemon.py` | Daemon que mantém os PTYs vivos e guarda a saída recente para reexibir |
| `matiz/protocol.py` | Protocolo binário simples entre janela e daemon |
| `matiz/procinfo.py` | Leitura de `/proc`: processo em primeiro plano, pasta, como retomar cada programa |
| `matiz/session.py` | Estado em disco (gravação atômica) e configuração |
| `matiz/usage.py`, `matiz/usage_ui.py` | Coleta e exibição do uso de IA |
| `matiz/claude_limits.py`, `bin/matiz-statusline` | Limites do plano do Claude via `statusLine` |

Arquivos gerados em tempo de execução (nenhum fica dentro do repositório):

| Caminho | Conteúdo |
|---|---|
| `~/.local/state/matiz/session.json` | Abas, cores, layout dos painéis, comando para retomar |
| `~/.local/state/matiz/scrollback/` | Histórico de texto de cada painel |
| `~/.local/state/matiz/claude-limits.json` | Últimos percentuais dos limites do Claude |
| `$XDG_RUNTIME_DIR/matiz/` | Socket e log do daemon |

O estado é gravado a cada 2 s de forma atômica, ao fechar a janela e ao receber SIGTERM.

## Limitações

- Reiniciar o computador encerra os processos: o Matiz recria as sessões, mas uma resposta que
  o Claude estava gerando naquele momento é interrompida.
- A reexibição da tela ao reconectar usa os últimos 4 MB de saída de cada painel.
- Depois de atualizar o código, feche todas as abas para que um daemon novo seja iniciado.
- Uma janela por vez.

## Testes

```bash
python3 -m unittest discover -s tests
```

Os testes do daemon e da detecção de processos criam processos de verdade e usam diretórios
temporários; nenhum toca na configuração do usuário.

## Licença

[MIT](LICENSE) © 2026 Eduardo Freitas
