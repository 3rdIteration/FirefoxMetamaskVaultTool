# Background
While tools like BTCRecover natively support extracting Metamask wallets from Chrome LevelDb files, Firefox data is stored in a different an incompatible way. Traditional Browser based methods for extracting the encrypted faults have also recently stopped working. This tool scans your system for Firefox profiles and attempts to retrieve the encrypted Metamask Vault.

Once found, you can use the encrytped vault with other tools. 

This tools is tested and works on Windows with Firefox 143.0.1

# How to use
1. Make sure you have Python 3 installed
2. Install the python packages `snappy` and `cramjam`
3. Run this script: `python firefox_metamask_seed_recovery.py`
4. If successful, something like this will be displayed:

   ```
   ---------------------------------------
   Probably found a Metamask vault:

   {"data":"m9b27bSJDFv5svrd7r76v/98nnv678b4TG6v8m+k0v998vnFf98nvfd9f==","iv":"8bbsvdG/G453==","salt":"AS6D/faas+8JJSD="}

   ---------------------------------------
   ```

8. You can now use the Vault Decryptor: https://metamask.github.io/vault-decryptor/ or BTCRecover https://github.com/3rdIteration/btcrecover/


If you recover funds, you may also want to tip the original author of this extraction script: `0xC5e9aCcd70FaEdafbe28D8b83DCCf5d3E9C8E527`
