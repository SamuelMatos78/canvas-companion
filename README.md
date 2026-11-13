# Canvas Companion — coleta do Canvas e apoio por IA

## Versão para alunos

O Canvas Companion em App_for_users/ oferece uma interface local com modo
noturno, configuração guiada do Canvas, escolha de provedor de IA, busca de
arquivos e atividades, automações e validade local de seis meses por curso. O
projeto gera executáveis standalone para Windows e Linux, além dos instaladores
de fonte e de um ZIP enxuto para publicar o código no Google Drive. O arquivo
`dist/canvas-companion-source.zip` pode ser enviado a alguém no Windows para compilar
o `.exe`; os passos estão em App_for_users/README.md.

O projeto sincroniza materiais e atividades do Canvas, cria uma area limpa para
pratica e, quando uma LLM OpenAI-compatible estiver disponivel, gera uma
resposta recomendada separada. Ele **nao envia respostas nem altera atividades
no Canvas**.

## Download do app
Você poderá baixar o app no site: [https://samuelmatos.dev/#tp=canvas-companion]
## Instalacao limpa

No PowerShell, a partir desta pasta:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

OCR de PDF tambem requer Poppler e Tesseract instalados no Windows. O script
`preparos/prep.ps1` instala o Poppler; o executavel do Tesseract precisa estar no
`PATH`.

## 1. Configurar e coletar atividades do Canvas

Copie `canvas_integracao/resumidor/res.env.example` para
`canvas_integracao/resumidor/res.env` e preencha `TOKEN_CANVAS`. O arquivo real
esta ignorado pelo Git.

O coletor recebe a URL visual que voce usa no navegador e a converte
internamente:

| URL visual | Endpoint consultado |
| --- | --- |
| `/courses/283059/assignments/` | `/api/v1/courses/283059/assignments` |
| `/courses/283059/assignments/123` | `/api/v1/courses/283059/assignments/123` |

Para buscar somente as atividades do curso indicado:

```powershell
python canvas_integracao/resumidor/resumidor.py --course-url "https://pucminas.instructure.com/courses/283059/assignments/" --only-assignments
```

Para validar metadados sem baixar anexos, e incluir mais de uma materia:

```powershell
python canvas_integracao/resumidor/resumidor.py --course-url "https://pucminas.instructure.com/courses/283059/assignments/" --course-id OUTRO_ID --only-assignments --metadata-only
```

Sem `--course-url` ou `--course-id`, o programa percorre os cursos ativos. Use
`--refresh-courses` para renovar o cache `cursos.json`. A listagem e paginada;
cada atividade recebe uma consulta individual de detalhe e o estado da
submissao do usuario atual.

Se um arquivo responder 403 ou falhar por outro motivo, o erro e exibido e o
coletor continua com os proximos arquivos, secoes e cursos. Ao final, o processo
retorna codigo `1` quando houve itens ignorados, permitindo detectar uma coleta
parcial sem interrompe-la no primeiro erro.

## 2. Criar o ambiente limpo de pratica

Este modo nao chama LLM e nao exige `OPENAI_API_KEY`:

```powershell
python exercitor/fazer_exerc.py --prepare-only
```

Dentro de cada atividade, ele cria:

```text
ambiente_pratica/
  enunciado.md
  arquivos/          # copia preservada dos anexos originais
  projetos/          # conteudo de projetos ZIP, sem sobrescrever seu trabalho
```

Arquivos que voce ja criou ou alterou em `ambiente_pratica` nao sao
sobrescritos numa nova execucao. ZIPs passam por limites de quantidade/tamanho e
por validacao contra caminhos maliciosos antes da extracao.

## 3. Gerar a resposta recomendada com uma LLM local

Copie `exercitor/.env.example` para `exercitor/.env` e configure, por exemplo:

```dotenv
OPENAI_PROVIDER=local
OPENAI_BASE_URL=http://127.0.0.1:8080/v1
OPENAI_MODEL=
OPENAI_API_KEY=
```

`OPENAI_MODEL` vazio escolhe o primeiro modelo retornado por `/v1/models`.
Preencha-o com um ID exato para escolher outro modelo. `OPENAI_API_KEY` e
opcional. `OPENAI_CHAT_COMPLETIONS_URL` aceita uma rota completa quando o
servidor nao segue `BASE_URL + /chat/completions`. Entao rode:

```powershell
python exercitor/fazer_exerc.py
```

A resposta fica isolada do material de pratica:

```text
resolucao_ia/
  resultado_ia.json          # contrato estruturado + metadados de entrega
  resultado_ia.md            # documento legivel, derivado da resposta Markdown
  resultado_ia.pdf           # PDF vetorial e paginado, pronto para revisao
  resposta_para_canvas.html  # somente para online_text_entry
```

A LLM continua obrigada a devolver JSON valido. Dentro dele,
`resultado.resposta_sugerida` e uma string Markdown completa; a conversao para
arquivo ou texto online acontece depois, sem pedir que o modelo misture JSON,
HTML e PDF. O bloco `entrega_canvas` registra os tipos aceitos, o tipo escolhido,
o arquivo principal e os parametros correspondentes da API do Canvas. Nenhum
conteudo e enviado automaticamente.

Para reconverter respostas que ja existem sem chamar a LLM novamente:

```powershell
python exercitor/fazer_exerc.py --export-existing
```

Arquivos de codigo-fonte e os textos dentro de ZIPs entram no contexto da LLM.
A pasta `resolucao_ia` e uma resposta recomendada final para revisao; o programa
nao altera automaticamente o codigo do projeto nem envia a entrega ao Canvas.

As variaveis `LLM_*` e as antigas `MARITACA_*` continuam aceitas como aliases,
mas novas configuracoes devem usar `OPENAI_*`.

## Resumir materiais de aula

O bibliotecario usa o mesmo cliente OpenAI-compatible:

```powershell
python bibliotecario/reader.py --root canvas_integracao/resumidor/downloads_canvas
```

Configure-o copiando `bibliotecario/.env.example` para `bibliotecario/.env`.

## Testes

Os testes automatizados nao chamam uma LLM real:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

O teste de integracao manual do Canvas deve usar `--metadata-only`, um diretorio
temporario e ao menos dois cursos. Nunca inclua `res.env`, downloads pessoais ou
respostas geradas no controle de versao.

Ha tambem um smoke test reproduzivel que valida lista, detalhe, submissao e o
CLI completo de criacao dos ambientes em duas materias, sem baixar anexos e sem
chamar LLM:

```powershell
python tests/live_canvas_smoke.py --course-url "https://pucminas.instructure.com/courses/283059/assignments/" --course-id OUTRO_ID
```

Acrescente `--download-one-per-course` para exigir e testar um anexo de ate
25 MiB por materia e sua copia para o ambiente de pratica. Os dados temporarios
sao apagados ao final.

## Painel integrado

O `Canvas Companion` reúne agenda, matérias, biblioteca e todas as automações em uma
interface local. Para abrir, dê dois cliques em:

```text
dashboard\ABRIR_PUC_CENTRAL.cmd
```

Ele mostra os dados já baixados, verifica a LLM local e permite atualizar os
conteúdos sem enviar ou alterar entregas no Canvas.

O executável e a versão em script usam a mesma biblioteca:
`%LOCALAPPDATA%\PUC Central\data` no Windows e `~/.local/share/puc-central/data`
no Linux (ou `PUC_DATA_ROOT`, se definido). Instalações antigas que ainda têm
`canvas_integracao/resumidor/downloads_canvas` continuam usando essa pasta até
que ela seja movida para o local comum.

A tela **Execuções** acompanha cada automação: etapa atual, progresso por curso,
atividade ou PDF, contadores (baixados, copiados, já existentes, erros), lista de
problemas com explicação, log completo filtrável, cancelamento e "Executar
novamente". Enquanto uma automação roda, as outras podem ser colocadas em uma
fila, executada em ordem, que pode ser reorganizada ou esvaziada na mesma tela.
A fila fica na memória e é descartada ao fechar o app. O histórico fica em `execucoes/` dentro da pasta de dados e inclui as
execuções iniciadas pela CLI. Uma coleta em que só alguns arquivos falham aparece
como "Concluída com avisos".

No Ubuntu, prepare e abra o painel com:

```bash
cd dashboard
npm ci
chmod +x iniciar_dashboard.sh ABRIR_PUC_CENTRAL.desktop
./iniciar_dashboard.sh
```

Também é possível abrir `dashboard/ABRIR_PUC_CENTRAL.desktop` pelo gerenciador
de arquivos. O backend agora reconhece ambientes virtuais tanto em
`Scripts/python.exe` (Windows) quanto em `bin/python3` (Ubuntu/Linux).

## CLI integrada

As mesmas consultas e automacoes do painel tambem estao disponiveis por uma
unica interface de terminal:

```powershell
puc
puc debug
python puc.py status
python puc.py entregas
python puc.py update
python puc.py assignments
python puc.py prepare
python puc.py solve
python puc.py export
python puc.py summarize
```

`python puc.py entregas` retorna um array JSON com `data_entrega`, `descricao`,
`nome_atividade` e `materia`. A janela padrão é de 14 dias e a descrição é
limitada a 100 caracteres. Os limites podem ser alterados com `--dias` e
`--limite-descricao` (ou `--days` e `--description-limit`).

As operacoes de exercicios e resumos tambem aceitam execucao direcionada:

```powershell
python puc.py solve --course PROGRAMACAO_2 --assignment-id 1413883
python puc.py prepare --course PROGRAMACAO_2 --assignment-id 1413883
python puc.py export --course PROGRAMACAO_2 --assignment-id 1413883
python puc.py summarize --course PROGRAMACAO_2
```

Sem `--assignment-id`, `solve`, `prepare` e `export` processam somente a materia
selecionada. Use `--force` em `solve` ou `summarize` para substituir resultados
existentes.

`python puc.py actions` lista todas as operacoes. Os aliases em portugues
(`atualizar`, `atividades`, `preparar`, `responder`, `converter` e `resumir`)
tambem sao aceitos. As execucoes no terminal sao sincronas, exibem a saida em
tempo real e retornam o mesmo codigo de saida usado pelos scripts internos.

Para diagnosticar Python e dependências sem preparar um ambiente virtual,
extraia o ZIP do código-fonte e rode `py -3 puc.py debug` no Windows ou
`python3 puc.py debug` no Linux.

No painel, a secao **Execucao direcionada** oferece os mesmos filtros. A
interface tambem possui modo noturno, escolhido automaticamente pela preferencia
do sistema na primeira abertura e salvo neste dispositivo depois da alternancia
manual.

-> Colaborador de porte para Windows: [https://github.com/ViniciusRezendeA]