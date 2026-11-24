# Migrar Google Fotos para o iCloud

Script para Windows que leva suas fotos e vídeos do **Google Fotos** para o **iCloud Fotos**:

- **sem duplicatas:** o que já está no iCloud não é enviado de novo, mesmo que o Google tenha guardado uma versão comprimida;
- **com data, hora e local:** as informações que o Google guarda em arquivos `.json` separados são gravadas dentro de cada foto, então tudo aparece em ordem cronológica;
- **sem a Lixeira:** fotos que você apagou no Google Fotos ficam de fora;
- **sem risco aos originais:** o script nunca altera nem apaga nada no Takeout ou no iCloud. Ele trabalha só com cópias.

> Projeto independente, sem relação com Google ou Apple.

## Como funciona

1. `analisar` lê o Takeout e a sua biblioteca do iCloud, compara as duas e gera um relatório com o que é **novo**, o que **já está no iCloud** e o que precisa de **revisão**.
2. `preparar` cria numa única pasta cópias das fotos novas, com data, hora e local gravados. Fotos HEIC viram JPEG, porque o site do iCloud não aceita HEIC.
3. Você arrasta essa pasta para [icloud.com/photos](https://www.icloud.com/photos) e confere o total que o script informou.

### Como as duplicatas são detectadas

| Situação | Resultado |
|---|---|
| Arquivo idêntico no iCloud | Não envia |
| Mesma foto, mesmo horário, outra qualidade ou formato | Não envia |
| Vídeo com o mesmo horário e a mesma duração | Não envia |
| Foto parecida com o mesmo horário (por exemplo, uma sequência) | Vai para revisão |
| Foto quase idêntica com data diferente | Vai para revisão |
| Sem data confiável | Vai para revisão |
| Original de uma foto editada no Google | Vai para revisão (a editada é enviada) |

Itens em revisão **não são enviados**, a menos que você escreva `enviar` na coluna `decisao` do arquivo `revisar.csv`.

## Requisitos

- Windows 10 ou 11
- [Python 3.10 ou mais novo](https://www.python.org/downloads/)
- [ExifTool](https://exiftool.org/): baixe o pacote do Windows, renomeie `exiftool(-k).exe` para `exiftool.exe` e coloque a pasta em `%LOCALAPPDATA%\Programs\ExifTool`, no `PATH` ou indique o caminho na variável de ambiente `EXIFTOOL`
- iCloud para Windows (Microsoft Store), com **Fotos** ativado
- Espaço livre em disco de cerca de 3 vezes o tamanho do Takeout

Instale as bibliotecas Python:

```powershell
python -m pip install -r requirements.txt
```

## Passo a passo

### 1. Exporte o Google Fotos

Em [takeout.google.com](https://takeout.google.com), selecione apenas **Google Fotos**, escolha arquivos `.zip` e baixe todas as partes para uma pasta que tenha **somente** esses arquivos. O script extrai todos os `.zip` dessa pasta.

### 2. Baixe a biblioteca do iCloud para o PC

A comparação precisa ler as fotos do iCloud. No Explorador de Arquivos, clique com o botão direito na pasta **iCloud Fotos** e escolha **Sempre manter neste dispositivo**. Espere o download terminar. Se algum arquivo ainda estiver só na nuvem, o `analisar` para e avisa, para não comparar com uma biblioteca incompleta.

Confira também se o total de itens no iPhone é igual ao número de arquivos na pasta. Se não for, o iCloud ainda está sincronizando.

### 3. Analise

```powershell
python migrar_fotos.py analisar --takeout "C:\Takeout" --saida "C:\MigracaoFotos"
```

- `--icloud` indica outra pasta de fotos do iCloud, se a sua não estiver em `%USERPROFILE%\Pictures\iCloud Photos\Photos`.
- `--fuso -03:00` define o fuso das fotos sem fuso gravado. O padrão é o fuso atual do Windows.
- `--aceitar-videos-na-nuvem` segue mesmo se alguns **vídeos** do iCloud não baixarem. Qualquer vídeo do Google que possa ser um deles vai para revisão.

Use uma pasta de saída com caminho curto, como `C:\MigracaoFotos`, para evitar o limite de tamanho de caminho do Windows.

### 4. Revise o relatório

Abra `relatorio.html` na pasta de saída. Ele mostra os totais e, lado a lado, os itens em revisão: Google Fotos à esquerda, iCloud à direita. Para enviar um item em revisão, escreva `enviar` na coluna `decisao` de `revisar.csv`.

### 5. Prepare as cópias

```powershell
python migrar_fotos.py preparar --saida "C:\MigracaoFotos"
```

O script grava data, hora e local em cada cópia, relê o arquivo para conferir e informa quantos itens o iCloud deve ter depois do envio. Erros ficam em `erros_preparo.csv`.

### 6. Envie pelo site

Abra a pasta `para_enviar`, selecione tudo com `Ctrl+A` e arraste para [icloud.com/photos](https://www.icloud.com/photos). Deixe a aba aberta até o envio terminar, atualize a página e confira o total (fotos + vídeos) com o que o script informou.

Se o navegador tiver dificuldade com muitos arquivos de uma vez, envie em partes.

### 7. Antes de apagar algo do Google Fotos

Espere o iPhone sincronizar e confira se ele mostra o mesmo total do site. Só então apague as fotos do Google, se quiser.

## Alternativa: pasta do iCloud para Windows

Em vez do site, você pode copiar as fotos para a pasta do iCloud para Windows, mantendo os HEIC originais:

```powershell
python migrar_fotos.py preparar --saida "C:\MigracaoFotos" --manter-heic
python migrar_fotos.py enviar --saida "C:\MigracaoFotos"
python migrar_fotos.py enviar --saida "C:\MigracaoFotos" --confirmar
python migrar_fotos.py conferir --saida "C:\MigracaoFotos"
```

Sem `--confirmar`, o `enviar` só simula. Ele copia em lotes de 500 (`--lote`) e pode ser repetido sem duplicar.

**Atenção:** em testes reais, o iCloud para Windows aceitou os arquivos na pasta, mas falhou ao enviá-los para a nuvem com erros de rede (`Put container(s) failed`). Envie primeiro um lote pequeno, como `--lote 10`, e confirme em icloud.com que ele chegou antes de mandar o resto.

## Limitações

- **Live Photos e Fotos com Movimento** podem chegar como uma foto e um vídeo separados.
- **Horário de verão:** fotos sem data interna recebem a data do Google com um fuso fixo. A ordem cronológica fica certa, mas a hora exibida pode ficar 1 hora diferente em períodos de horário de verão.
- **HEIC convertido em JPEG** perde o efeito HDR das fotos que o têm. A diferença de qualidade em tela é imperceptível.
- **RAW e `.mp`** (vídeos de Motion Photo do Google) vão para revisão.
- O script foi feito e testado no **Windows**.

## Arquivos gerados

| Arquivo | Conteúdo |
|---|---|
| `relatorio.html` | Resumo e itens para revisão, com miniaturas |
| `relatorio.csv` | Todos os itens, com categoria, motivo e data |
| `revisar.csv` | Itens em revisão e a coluna `decisao` |
| `para_enviar/` | Cópias prontas para enviar |
| `manifesto.json` | Controle do que foi preparado e enviado |
| `indice.sqlite` | Cache da leitura, para rodar de novo mais rápido |

## Testes

Os testes criam fotos e vídeos sintéticos numa pasta temporária e rodam todas as etapas. Nenhuma foto real é usada.

```powershell
python tests\teste_ponta_a_ponta.py
```

## Licença

[MIT](LICENSE)
