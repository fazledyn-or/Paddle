/* Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License. */

#include "paddle/fluid/operators/fused/fusion_seqpool_concat_op.h"

#include <string>
#include <vector>

#include "paddle/phi/kernels/funcs/jit/kernels.h"

namespace paddle {
namespace operators {

void FusionSeqPoolConcatOp::InferShape(
    framework::InferShapeContext* ctx) const {
  PADDLE_ENFORCE_GE(ctx->Inputs("X").size(),
                    1UL,
                    platform::errors::InvalidArgument(
                        "Inputs(X) of FusionSeqPoolConcatOp should be greated "
                        "than 1, but received value is %d.",
                        ctx->Inputs("X").size()));
  OP_INOUT_CHECK(ctx->HasOutput("Out"), "Output", "Out", "FusionSeqPoolConcat");
  int axis = ctx->Attrs().Get<int>("axis");
  PADDLE_ENFORCE_EQ(axis,
                    1,
                    platform::errors::InvalidArgument(
                        "FusionSeqPoolConcatOp only supports concat "
                        "axis=1 yet, but received axis value is %d",
                        axis));

  auto ins_dims = ctx->GetInputsDim("X");
  const size_t n = ins_dims.size();
  PADDLE_ENFORCE_GT(n,
                    0UL,
                    platform::errors::InvalidArgument(
                        "Input tensors count should be greater than 0, "
                        "but received value is %d.",
                        n));
  if (n == 1) {
    LOG(WARNING) << "Only have one input, may waste memory";
  }

  // The output height should be confirmed in Compute,
  // since input lod is not accessible here.
  PADDLE_ENFORCE_EQ(ins_dims[0].size(),
                    2,
                    platform::errors::InvalidArgument(
                        "The dims size of first input should be equal to 2, "
                        "but received value is %d.",
                        ins_dims[0].size()));
  ctx->SetOutputDim("Out", {-1, ins_dims[0][axis] * static_cast<int>(n)});

  if (!ctx->IsRuntime()) {
    // when compiling, the LodLevel of Out is set to be 1, which is consistent
    // with that in running time.
    ctx->SetLoDLevel("Out", 1);
  }
}

phi::KernelKey FusionSeqPoolConcatOp::GetExpectedKernelType(
    const framework::ExecutionContext& ctx) const {
  return phi::KernelKey(OperatorWithKernel::IndicateVarDataType(ctx, "X"),
                        ctx.GetPlace());
}

void FusionSeqPoolConcatOpMaker::Make() {
  AddInput("X", "(phi::DenseTensor) Input tensors of this operator.")
      .AsDuplicable();
  AddOutput("Out", "(phi::DenseTensor) Output tensor of concat operator.");
  AddAttr<std::string>("pooltype",
                       "(string, default 'SUM') some of the pooling "
                       "pooltype of SequencePoolOp.")
      .SetDefault("SUM")
      .InEnum({"AVERAGE", "SUM", "SQRT"});
  AddAttr<int>("axis",
               "The axis along which the input tensors will be concatenated. "
               "Only supports concat axis=1 yet.")
      .SetDefault(1);
  AddComment(R"DOC(
Fusion Sequence Pool of pooltype(sum, average and sqrt) and Concat Operator.
)DOC");
}

template <typename T, typename DeviceContext>
class FusionSeqPoolConcatKernel : public framework::OpKernel<T> {
 public:
  void Compute(const framework::ExecutionContext& ctx) const override {
    auto ins = ctx.MultiInput<phi::DenseTensor>("X");
    auto* out = ctx.Output<phi::DenseTensor>("Out");
    std::string pooltype = ctx.Attr<std::string>("pooltype");
    auto x0_lod = ins[0]->lod();
    const auto& x0_dims = ins[0]->dims();
    const auto& y_dims = out->dims();
    size_t bs = x0_lod[0].size() - 1;
    out->Resize({static_cast<int64_t>(bs), y_dims[1]});
    framework::LoD y_lod(1);
    y_lod[0].resize(bs + 1);
    for (size_t i = 0; i <= bs; ++i) {
      y_lod[0][i] = i;
    }
    out->set_lod(y_lod);
    auto place = ctx.GetPlace();
    T* y_data = out->mutable_data<T>(place);

    int w = static_cast<int>(ins[0]->numel() / x0_dims[0]);
    PADDLE_ENFORCE_EQ(y_dims[1] % w,
                      0,
                      platform::errors::InvalidArgument(
                          "The output of dims[1] should be dividable of w, but "
                          "dims[1] is %d, w is %d.",
                          y_dims[1],
                          w));
    phi::jit::seq_pool_attr_t attr(w, phi::jit::SeqPoolType::kSum);
    if (pooltype == "AVERAGE") {
      attr.type = phi::jit::SeqPoolType::kAvg;
    } else if (pooltype == "SQRT") {
      attr.type = phi::jit::SeqPoolType::kSqrt;
    }
    auto seqpool = phi::jit::KernelFuncs<phi::jit::SeqPoolTuple<T>,
                                         platform::CPUPlace>::Cache()
                       .At(attr);
    size_t n = ins.size();
    size_t dst_step_size = n * w;
    for (size_t i = 0; i < n; ++i) {
      const auto& x_dims = ins[i]->dims();
      auto x_lod = ins[i]->lod()[0];
      const T* src = ins[i]->data<T>();
      T* dst = y_data + i * w;
      PADDLE_ENFORCE_EQ(
          static_cast<int>(ins[i]->numel() / x_dims[0]),
          w,
          platform::errors::InvalidArgument(
              "Width of all inputs should be equal, but the width of the %d-th "
              "input %d is not equal to the previous %d",
              i,
              static_cast<int>(ins[i]->numel() / x_dims[0]),
              w));
      PADDLE_ENFORCE_EQ(
          x_lod.size(),
          bs + 1,
          platform::errors::InvalidArgument(
              "Batchsize of all inputs should be equal, but the value of the "
              "%d-th %d is not equal to the previous %d.",
              i,
              x_lod.size(),
              bs + 1));
      for (size_t j = 0; j < bs; ++j) {
        attr.h = static_cast<int>(x_lod[j + 1] - x_lod[j]);
        seqpool(src, dst, &attr);
        dst += dst_step_size;
        src += attr.h * attr.w;
      }
    }
  }
};

}  // namespace operators
}  // namespace paddle

namespace ops = paddle::operators;
REGISTER_OPERATOR(fusion_seqpool_concat,
                  ops::FusionSeqPoolConcatOp,
                  ops::FusionSeqPoolConcatOpMaker);

PD_REGISTER_STRUCT_KERNEL(fusion_seqpool_concat,
                          CPU,
                          ALL_LAYOUT,
                          ops::FusionSeqPoolConcatKernel,
                          float,
                          double) {}
