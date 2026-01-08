from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# Explicitly collect all submodules in pyhako to ensure nothing is dropped
hiddenimports = collect_submodules('pyhako')
datas = collect_data_files('pyhako')



