社内プロキシなどで独自の CA を使っている環境では、その CA 証明書(PEM、拡張子 .crt)をこのフォルダに置いてから
`docker compose build` する。イメージ内で信頼され、pip のインストールも通る。証明書は git に入れない(.gitignore 済み)。
