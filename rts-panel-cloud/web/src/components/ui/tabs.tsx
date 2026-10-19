/**
 * Tabs（shadcn/ui 样式，手写）。用于过门区的「门内 / 门外」两态切换。
 *
 * ⚠ Radix v1 的导入面是**扁平命名导出**（`Root`/`List`/`Trigger`/`Content`），
 * 老写法 `TabsPrimitive.List` 会直接 TS 报错（实测 @radix-ui/react-tabs 1.2.1）。
 */

import type * as React from "react";

import { Root as TabsRoot, List as TabsListPrimitive, Trigger as TabsTriggerPrimitive, Content as TabsContentPrimitive } from "@radix-ui/react-tabs";

import { cn } from "@/lib/utils";

function Tabs({ className, ...props }: React.ComponentProps<typeof TabsRoot>) {
  return <TabsRoot data-slot="tabs" className={cn("flex flex-col gap-2", className)} {...props} />;
}

function TabsList({ className, ...props }: React.ComponentProps<typeof TabsListPrimitive>) {
  return (
    <TabsListPrimitive
      data-slot="tabs-list"
      className={cn(
        "inline-flex h-7 w-fit items-center justify-center gap-1 rounded-md bg-white/5 p-0.5",
        className,
      )}
      {...props}
    />
  );
}

function TabsTrigger({ className, ...props }: React.ComponentProps<typeof TabsTriggerPrimitive>) {
  return (
    <TabsTriggerPrimitive
      data-slot="tabs-trigger"
      className={cn(
        "inline-flex h-6 flex-1 items-center justify-center rounded-sm px-2 text-[12px] font-medium whitespace-nowrap text-text-dim transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50 data-[state=active]:bg-card data-[state=active]:text-text",
        className,
      )}
      {...props}
    />
  );
}

function TabsContent({ className, ...props }: React.ComponentProps<typeof TabsContentPrimitive>) {
  return (
    <TabsContentPrimitive
      data-slot="tabs-content"
      className={cn("flex-1 outline-none", className)}
      {...props}
    />
  );
}

export { Tabs, TabsList, TabsTrigger, TabsContent };
