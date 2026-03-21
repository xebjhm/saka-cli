from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# Explicitly collect all submodules in pysaka to ensure nothing is dropped
hiddenimports = collect_submodules("pysaka")
datas = collect_data_files("pysaka")
