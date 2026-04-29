# Customer A sample
## Inventory:
Number of VM is off: 2831 actual vs. 2661 in app
Looks like the logic looks at Powered on VMs only for total count
Powered on VMs match: 2661 actual vs. 2661 in app
ESX host number is off: 242 actual vs. 280 in app
Total vCPU: 15,330 actual vs. 14,628 in app
Counting only powered on VMs
RAM: Counting only powered on 67,989 actual vs. 67,989 in app
Storage: in vPartition, capacity GB 4,389,810 (x/953.6743) actual vs. 4,034,346 GB in app
vCPU/core ratio: 1.58 actual vs. 1.52x in app
Windows pCOres: 7,817 actual vs. 7,836 using 1.52 ratio for both
ESU pCores (2012 older): 710 actual vs. 712 using 1.52
Add the same written assumption for ESU SQL.
 
## Analysis
402 errors in xa2.  How are we treating unmatched VMs and storage in app?
Azure vCPU: 16,318 actual vs. 11,872 in app
This should be with 8 core min
Azure RAM: 76,068 actual vs. 67989 in app
Azure Storage: vPartition Capacity GB 4,572,825 GB actual vs. 3,081,602 GB in app
 
## Azure cost
Compute PayG: $6,704,247 actual vs. $2,622,113 in app (app might be using RI pricing no PayG)
Can we change to another purchasing option like 1 and 3 yr RI?
Storage cost: $4,115,545 actual vs. $3,638,545
 
## Financial Metrics
Using the data from the app, the benchmark model returned all different values, so perhaps we need to revisit the logic behind the app calculations. 
