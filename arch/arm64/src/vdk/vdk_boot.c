/****************************************************************************
 * arch/arm64/src/vdk/vdk_boot.c
 *
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.  The
 * ASF licenses this file to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance with the
 * License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
 * WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
 * License for the specific language governing permissions and limitations
 * under the License.
 *
 ****************************************************************************/

/****************************************************************************
 * Included Files
 ****************************************************************************/

#include <nuttx/config.h>

#include <stdint.h>
#include <assert.h>
#include <debug.h>

#include <arch/chip/chip.h>

#ifdef CONFIG_SMP
#include "arm64_smp.h"
#endif

#include "arm64_arch.h"
#include "arm64_internal.h"
#include "arm64_mpu.h"
#include "chip.h"
#include "vdk_boot.h"
#include "vdk_serial.h"

/****************************************************************************
 * Private Data
 ****************************************************************************/

static const struct arm64_mpu_region g_mpu_regions[] =
{
  /* Region 0 NuttX text */

  MPU_REGION_ENTRY("nx_code",
     (uint64_t)_stext,
     (uint64_t)_etext,
     REGION_RAM_TEXT_ATTR),

  /* Region 1 NuttX rodata */

  MPU_REGION_ENTRY("nx_rodata",
     (uint64_t)_srodata,
     (uint64_t)_erodata,
     REGION_RAM_RO_ATTR),

  /* Region 2 NuttX data */

  MPU_REGION_ENTRY("nx_data",
     (uint64_t)_sdata,
     (uint64_t)CONFIG_RAMBANK_END,
     REGION_RAM_ATTR),

  /* Region 3 device region */

  MPU_REGION_ENTRY("DEVICE1",
     (uint64_t)CONFIG_DEVICEIO1_BASEADDR,
     (uint64_t)CONFIG_DEVICEIO1_END,
     REGION_DEVICE_ATTR),

  /* Region 4 sram region */

  MPU_REGION_ENTRY("SRAM",
     (uint64_t)CONFIG_SRAM_BASEADDR,
     (uint64_t)CONFIG_SRAM_END,
     REGION_RAM_ATTR)
};

const struct arm64_mpu_config g_mpu_config =
{
  .num_regions = nitems(g_mpu_regions),
  .mpu_regions = g_mpu_regions,
};

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/****************************************************************************
 * Name: arm64_el_init
 *
 * Description:
 *   The function called from arm64_head.S at very early stage for these
 * platform, it's use to:
 *   - Handling special hardware initialize routine which is need to
 *     run at high ELs
 *   - Initialize system software such as hypervisor or security firmware
 *     which is need to run at high ELs
 *
 ****************************************************************************/

void arm64_el_init(void)
{
  uint64_t reg;

  write_sysreg(CONFIG_SYS_COUNT_CLK_FREQ, cntfrq_el0);

  ARM64_ISB();

  reg = read_sysreg(s3_0_c15_c0_5);
  reg |= 0x3;
  write_sysreg(reg, s3_0_c15_c0_5); /* Enable SPP */
}

#ifdef CONFIG_ARCH_HAVE_MULTICPU

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/****************************************************************************
 * Name: arm64_get_mpid
 *
 * Description:
 *   The function from cpu index to get cpu mpid which is reading
 * from mpidr_el1 register. Different ARM64 Core will use different
 * Affn define, the mpidr_el1 value is not CPU number, So we need
 * to change CPU number to mpid and vice versa
 *
 ****************************************************************************/

uint64_t arm64_get_mpid(int cpu)
{
  return CORE_TO_MPID(cpu, 1);
}

/****************************************************************************
 * Name: arm64_get_cpuid
 *
 * Description:
 *   The function from mpid to get cpu id
 *
 ****************************************************************************/

int arm64_get_cpuid(uint64_t mpid)
{
  return MPID_TO_CORE(mpid);
}

#endif /* CONFIG_ARCH_HAVE_MULTICPU */

/****************************************************************************
 * Name: arm64_chip_boot
 *
 * Description:
 *   Complete boot operations started in arm64_head.S
 *
 ****************************************************************************/

void arm64_chip_boot(void)
{
  /* MAP IO and DRAM, enable MPU. */

  arm64_mpu_init(true);

#if defined(CONFIG_ARM64_PSCI)
  arm64_psci_init("smc");
#endif

  /* Perform board-specific device initialization. This would include
   * configuration of board specific resources such as GPIOs, LEDs, etc.
   */

  vdk_board_initialize();

#ifdef USE_EARLYSERIALINIT
  /* Perform early serial initialization if we are going to use the serial
   * driver.
   */

  arm64_earlyserialinit();
#endif

#ifdef CONFIG_ARCH_PERF_EVENTS
  up_perf_init((void *)CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC);
#endif
}
