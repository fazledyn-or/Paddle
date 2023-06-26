// Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
#pragma once

#include <map>
#include <vector>

#include "paddle/fluid/framework/new_executor/interpreter/plan.h"
#include "paddle/fluid/framework/program_desc.h"

namespace paddle {
namespace framework {

void SetColAttrForFetchOps(const interpreter::Job& job,
                           std::shared_ptr<ProgramDesc> program_desc);

}  // namespace framework
}  // namespace paddle