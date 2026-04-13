outputs 文件夹说明
==================

xhs-runs\
  每次成功执行 bench-hermes-xhs-sync.ps1（且未加 -NoExport）后，会在这里生成一篇 .txt。
  若文件夹是空的：请先跑一次 bench，或确认没有用 -NoExport。

xhs-articles-log.txt（在 outputs 根目录）
  所有运行会追加写入这一份汇总日志。

若你看不到 xhs-runs：在仓库根目录执行一次  .\bench-hermes-xhs-sync.ps1
即可自动创建并写入。
