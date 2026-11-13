# Canvas Companion

Aplicação local para estudantes que usam Canvas. O portfólio pode distribuir o executável pronto ou os pequenos instaladores; estes baixam o ZIP público do código-fonte no Google Drive. A interface é servida pelo próprio aplicativo local.

## O que o aluno consegue fazer

- Conectar uma instância do Canvas com a URL e um token pessoal.
- Sincronizar cursos, prazos, módulos e arquivos para o computador.
- Pesquisar atividades, itens de módulos e arquivos baixados.
- Perguntar ao assistente sobre os materiais sincronizados. O modelo tem ferramentas somente de leitura para pesquisar os dados locais. O modelo escolhido e o servidor configurado precisam oferecer suporte a chamadas de ferramentas.
- Escolher OpenAI/GPT, Claude, Gemini, modelos locais ou endpoints compatíveis.
- Preparar ambientes de exercício, gerar rascunhos recomendados, converter arquivos e resumir PDFs.

Os envios ao Canvas continuam manuais. O assistente não envia respostas nem altera atividades no Canvas.

## Publicação no portfólio

Gere o ZIP do código, os instaladores de fonte e o executável do sistema atual:

    python3 preparos/empacotar_fonte.py
    python3 -m pip install -r requirements-build.txt
    python3 preparos/build_executable.py

Envie `dist/canvas-companion-source.zip` ao Google Drive e configure o compartilhamento como “qualquer pessoa com o link”. Depois gere os instaladores já configurados com esse endereço:

    python3 preparos/empacotar_fonte.py --source-url "https://drive.google.com/file/d/ID_DO_ARQUIVO/view"

Publique `dist/canvas-companion-windows-installer.zip` e `dist/canvas-companion-linux-install.sh` no portfólio. O pacote Windows contém `install.cmd` e `install.ps1`; o aluno extrai o ZIP e dá dois cliques em `install.cmd`. O Linux recebe um único script. Com `--source-url`, os alunos não precisam abrir o Drive nem copiar o link; sem ele, o instalador pergunta o endereço.

O build independente gera `dist/executables/Canvas-Companion` no Linux ou `dist/executables/Canvas-Companion.exe` no Windows. Ele inclui o runtime Python e não exige instalação de Python no computador do aluno. Compile cada formato no sistema correspondente. O workflow `.github/workflows/build-executables.yml` cria os dois artefatos em runners Linux e Windows quando executado em um repositório GitHub.

### Pedir a um amigo para compilar o `.exe` no Windows

No Linux, envie somente `dist/canvas-companion-source.zip`. Ele contém os fontes, a interface, `requirements-build.txt` e `preparos/build_executable.py`. Não envie o binário Linux, as pastas `venv`, arquivos `.env`, tokens ou dados do Canvas.

Seu amigo precisa de Windows 10/11 de 64 bits, Python 3.12 e internet para baixar as dependências. Ele deve extrair o ZIP, abrir o PowerShell na pasta `canvas-companion` extraída e executar:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe preparos\build_executable.py
```

O executável ficará em `dist\executables\Canvas-Companion.exe`. Para compilar a versão Windows, ele deve fazer o build no próprio Windows. O ZIP de fonte já contém todos os arquivos necessários para esse build manual; a pasta `.github` não precisa ser enviada.

O empacotador inclui somente os arquivos de código necessários, a interface e os instaladores. Ele deixa de fora caches e arquivos baixados do Canvas, chaves, configurações locais, respostas produzidas, ambientes virtuais e dependências web.

## Chaves e dados pessoais

As chaves do Canvas e do provedor de IA são gravadas no arquivo de configuração do usuário no computador. No Linux, o app restringe as permissões do arquivo; no Windows, ele fica dentro do perfil do usuário. O arquivo não é criptografado. O servidor local escuta somente em 127.0.0.1.

Quando o aluno usa o assistente com um provedor remoto, a pergunta e os trechos dos materiais necessários para respondê-la são enviados à API desse provedor. O app também oferece modelos locais para quem não quer enviar esse conteúdo a um serviço remoto.

O usuário paga diretamente os custos de sua API de IA. Para o Canvas, crie um token pessoal com apenas as permissões necessárias e revogue-o nas configurações do Canvas quando não precisar mais.

## Validade de cursos

Cada curso recebe um prazo local de seis meses a partir da primeira sincronização neste computador. A tela mostra quando o acompanhamento vence; isso não altera a matrícula nem a disponibilidade real do curso no Canvas.

## Instalação

O executável standalone não exige instalação de Python nem privilégios de administrador. Os instaladores baseados em código-fonte continuam disponíveis como alternativa; eles requerem Python 3.10 ou superior e internet durante a primeira instalação e criam um ambiente privado do usuário.

Windows: deixe `install.cmd` e `install.ps1` juntos na mesma pasta e dê dois cliques em `install.cmd`. Se o instalador pedir, cole o link público do ZIP no Drive.

Linux:

    chmod +x install.sh
    ./install.sh

Com o executável, abra `Canvas-Companion` para iniciar a interface. Para depurar, abra um terminal na pasta e execute `./Canvas-Companion debug` no Linux ou `Canvas-Companion.exe debug` no Windows. Com o instalador de fonte, abra um terminal novo e rode `puc` para abrir a interface ou `puc debug` para ver o diagnóstico local. A CLI funciona sem Node.js. PDFs com texto selecionável podem ser resumidos sem componentes adicionais; PDFs digitalizados precisam de Poppler e Tesseract para OCR.

Para diagnosticar o computador sem instalar o ambiente virtual, extraia o ZIP de código-fonte e execute `py -3 puc.py debug` no Windows ou `python3 puc.py debug` no Linux, dentro da pasta extraída. Esse diagnóstico não mostra tokens nem chaves.
